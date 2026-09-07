package com.libraryhub;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import javax.xml.parsers.DocumentBuilderFactory;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

/**
 * Guards the build's quality-gate configuration against being silently weakened.
 *
 * <p>The POMs are rewritten by agents as the project grows, so every threshold configured there
 * is temporary unless something asserts it. Each gate below can be disabled without any build
 * output changing — a JaCoCo limit on the wrong execution, a missing PIT threshold, a Checkstyle
 * severity that lets every warning through. This test is what makes the next such loss a red
 * build instead of a quiet one.
 *
 * <p>Where a setting has a "looks configured but does nothing" failure mode, that is the one
 * asserted. {@code jacoco:report} never fails a build however its rules are written; only
 * {@code jacoco:check} does. A coverage minimum attached to the report execution parses fine and
 * gates nothing.
 *
 * <p>Scaffolded by Kiln, not authored per project. Extend it when a gate is added; do not relax a
 * threshold here to make a build pass — fix the code, or change the gate deliberately and say so
 * in the handoff (see constitution/engineering.md).
 */
@DisplayName("build quality-gate configuration")
class GateConfigTest {

    private static final double REQUIRED_COVERAGE = 0.90;
    private static final int REQUIRED_MUTATION_THRESHOLD = 80;

    /** Project root: the first ancestor holding a pom.xml with a &lt;modules&gt; section. */
    private static Optional<Path> reactorRoot() {
        Path dir = Path.of("").toAbsolutePath();
        for (int depth = 0; depth < 6 && dir != null; depth++, dir = dir.getParent()) {
            Path pom = dir.resolve("pom.xml");
            if (Files.isRegularFile(pom) && readText(pom).contains("<modules>")) {
                return Optional.of(dir);
            }
        }
        return Optional.empty();
    }

    private static String readText(Path path) {
        try {
            return Files.readString(path);
        } catch (Exception e) {
            return "";
        }
    }

    private static Document parse(Path pom) {
        try {
            DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
            factory.setNamespaceAware(false);
            return factory.newDocumentBuilder().parse(pom.toFile());
        } catch (Exception e) {
            throw new IllegalStateException("could not parse " + pom, e);
        }
    }

    /** Every &lt;plugin&gt; element in the POM whose artifactId matches. */
    private static List<Element> plugins(Document doc, String artifactId) {
        List<Element> found = new ArrayList<>();
        NodeList all = doc.getElementsByTagName("plugin");
        for (int i = 0; i < all.getLength(); i++) {
            Element plugin = (Element) all.item(i);
            if (artifactId.equals(childText(plugin, "artifactId"))) {
                found.add(plugin);
            }
        }
        return found;
    }

    /** Text of the first direct-or-nested child with this tag, or null. */
    private static String childText(Element parent, String tag) {
        NodeList matches = parent.getElementsByTagName(tag);
        if (matches.getLength() == 0) {
            return null;
        }
        return matches.item(0).getTextContent().trim();
    }

    private static List<Element> descendants(Element parent, String tag) {
        List<Element> found = new ArrayList<>();
        NodeList matches = parent.getElementsByTagName(tag);
        for (int i = 0; i < matches.getLength(); i++) {
            found.add((Element) matches.item(i));
        }
        return found;
    }

    /** All POMs in the reactor: the parent plus each declared module. */
    private static List<Path> reactorPoms(Path root) {
        List<Path> poms = new ArrayList<>();
        poms.add(root.resolve("pom.xml"));
        Document parent = parse(root.resolve("pom.xml"));
        NodeList modules = parent.getElementsByTagName("module");
        for (int i = 0; i < modules.getLength(); i++) {
            Path modulePom = root.resolve(modules.item(i).getTextContent().trim()).resolve("pom.xml");
            if (Files.isRegularFile(modulePom)) {
                poms.add(modulePom);
            }
        }
        return poms;
    }

    @Test
    @DisplayName("jacoco:check is bound and enforces a >= 90% line coverage minimum")
    void jacocoCheckIsBoundWithMinimum() {
        Optional<Path> maybeRoot = reactorRoot();
        assumeTrue(maybeRoot.isPresent(), "no reactor pom.xml yet - the project does not exist");
        Path root = maybeRoot.get();

        boolean checkGoalBound = false;
        boolean minimumSatisfied = false;

        for (Path pom : reactorPoms(root)) {
            for (Element plugin : plugins(parse(pom), "jacoco-maven-plugin")) {
                for (Element execution : descendants(plugin, "execution")) {
                    boolean isCheck = descendants(execution, "goal").stream()
                            .anyMatch(goal -> "check".equals(goal.getTextContent().trim()));
                    if (!isCheck) {
                        continue;
                    }
                    checkGoalBound = true;
                    for (Element limit : descendants(execution, "limit")) {
                        String counter = childText(limit, "counter");
                        String value = childText(limit, "value");
                        String minimum = childText(limit, "minimum");
                        if ("LINE".equals(counter) && "COVEREDRATIO".equals(value)
                                && minimum != null
                                && Double.parseDouble(minimum) >= REQUIRED_COVERAGE) {
                            minimumSatisfied = true;
                        }
                    }
                }
            }
        }

        assertTrue(checkGoalBound,
                "no jacoco-maven-plugin execution binds the 'check' goal. "
                        + "'report' never fails a build however its rules are written, "
                        + "so without 'check' there is no coverage gate.");
        assertTrue(minimumSatisfied,
                "the jacoco 'check' execution has no LINE/COVEREDRATIO limit of at least "
                        + REQUIRED_COVERAGE + ". A limit on the 'report' execution is ignored.");
    }

    @Test
    @DisplayName("jacoco has no coverage exclusions")
    void jacocoHasNoExcludes() {
        Optional<Path> maybeRoot = reactorRoot();
        assumeTrue(maybeRoot.isPresent(), "no reactor pom.xml yet - the project does not exist");

        List<String> excluded = new ArrayList<>();
        for (Path pom : reactorPoms(maybeRoot.get())) {
            for (Element plugin : plugins(parse(pom), "jacoco-maven-plugin")) {
                for (Element exclude : descendants(plugin, "exclude")) {
                    excluded.add(exclude.getTextContent().trim());
                }
            }
        }

        assertTrue(excluded.isEmpty(),
                "jacoco <excludes> is " + excluded + ", expected empty. Excluding a class "
                        + "removes it from the denominator instead of testing it; the acceptance "
                        + "suite covers the infrastructure adapters against real containers.");
    }

    @Test
    @DisplayName("PIT enforces a >= 80% mutation threshold")
    void pitestMutationThresholdIsEnforced() {
        Optional<Path> maybeRoot = reactorRoot();
        assumeTrue(maybeRoot.isPresent(), "no reactor pom.xml yet - the project does not exist");

        Integer threshold = null;
        for (Path pom : reactorPoms(maybeRoot.get())) {
            for (Element plugin : plugins(parse(pom), "pitest-maven")) {
                String configured = childText(plugin, "mutationThreshold");
                if (configured != null && !configured.isBlank()) {
                    threshold = Integer.parseInt(configured);
                }
            }
        }

        assertTrue(threshold != null && threshold >= REQUIRED_MUTATION_THRESHOLD,
                "pitest <mutationThreshold> is " + threshold + ", expected >= "
                        + REQUIRED_MUTATION_THRESHOLD
                        + ". Without it PIT reports a score and exits 0 at any value.");
    }

    @Test
    @DisplayName("Checkstyle uses a committed ruleset and fails on warnings")
    void checkstyleRulesetIsPinned() {
        Optional<Path> maybeRoot = reactorRoot();
        assumeTrue(maybeRoot.isPresent(), "no reactor pom.xml yet - the project does not exist");

        String configLocation = null;
        String violationSeverity = null;
        for (Path pom : reactorPoms(maybeRoot.get())) {
            for (Element plugin : plugins(parse(pom), "maven-checkstyle-plugin")) {
                String location = childText(plugin, "configLocation");
                if (location != null && !location.isBlank()) {
                    configLocation = location;
                }
                String severity = childText(plugin, "violationSeverity");
                if (severity != null && !severity.isBlank()) {
                    violationSeverity = severity;
                }
            }
        }

        assertTrue(configLocation != null && !configLocation.isBlank(),
                "maven-checkstyle-plugin has no <configLocation>. Without one the plugin falls "
                        + "back to its bundled default ruleset, which differs by plugin version.");
        assertTrue("warning".equalsIgnoreCase(violationSeverity),
                "checkstyle <violationSeverity> is " + violationSeverity
                        + ", expected 'warning'. The default of 'error' lets every "
                        + "warning-level rule pass without failing the build.");
    }

    @Test
    @DisplayName("each module has an ArchUnit architecture test")
    void everyModuleHasAnArchitectureTest() {
        Optional<Path> maybeRoot = reactorRoot();
        assumeTrue(maybeRoot.isPresent(), "no reactor pom.xml yet - the project does not exist");
        Path root = maybeRoot.get();

        Document parent = parse(root.resolve("pom.xml"));
        NodeList modules = parent.getElementsByTagName("module");
        assumeTrue(modules.getLength() > 0, "no modules declared yet");

        List<String> missing = new ArrayList<>();
        for (int i = 0; i < modules.getLength(); i++) {
            String module = modules.item(i).getTextContent().trim();
            Path testRoot = root.resolve(module).resolve("src/test/java");
            if (!Files.isDirectory(testRoot)) {
                continue;
            }
            if (!containsArchitectureTest(testRoot)) {
                missing.add(module);
            }
        }

        assertTrue(missing.isEmpty(),
                "modules with no ArchitectureTest: " + missing
                        + ". Layering is enforced by ArchUnit tests rather than by configuration, "
                        + "so a module without one has no layering gate and nothing reports that.");
    }

    private static boolean containsArchitectureTest(Path testRoot) {
        try (var paths = Files.walk(testRoot)) {
            return paths.anyMatch(p -> p.getFileName().toString().equals("ArchitectureTest.java"));
        } catch (Exception e) {
            return false;
        }
    }

    @Test
    @DisplayName("the seed catalog fixture matches the requirements")
    void seedFixtureIsUnchanged() {
        Optional<Path> maybeRoot = reactorRoot();
        assumeTrue(maybeRoot.isPresent(), "no reactor pom.xml yet - the project does not exist");
        Path fixture = maybeRoot.get()
                .resolve("catalog-service/src/test/java/com/libraryhub/catalog/SeedBooks.java");
        assumeTrue(Files.isRegularFile(fixture), "seed fixture not created yet");

        // Strip comments first: the fixture's own Javadoc legitimately names the values it warns
        // against, and matching on those would make the check pass or fail on prose.
        String source = readText(fixture)
                .replaceAll("(?s)/\\*.*?\\*/", "")
                .replaceAll("(?m)//.*$", "");
        for (String required : new String[] {
                "Dune", "Frank Herbert", "Sci-Fi", "978-0-20-163361-0",
                "Refactoring", "Martin Fowler", "Software", "978-0-13-468599-1",
                "The Hobbit", "J.R.R. Tolkien", "Fantasy", "978-3-16-148410-0"}) {
            assertTrue(source.contains(required),
                    "seed fixture is missing " + required + ". The catalog seed values are pinned "
                            + "in the requirements so a filter term matches at most one book; "
                            + "substituting them re-opens the ambiguity they were chosen to close.");
        }
        assertFalse(source.contains("Science Fiction"),
                "seed fixture uses 'Science Fiction'. The requirements pin 'Sci-Fi': genres "
                        + "sharing a substring make a single-match filter row unsatisfiable.");
    }
}
