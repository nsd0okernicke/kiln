# Project Rules — LibraryHub (Java)

## Language & Tooling

- Language: Java 21+
- Build tool: Maven, multi-module (parent POM + `catalog-service`, `loans-service`)
- Do not change another role's prompt or workflow ownership without explicit user direction.

## JDK and Maven Wrapper

The project uses the Maven wrapper committed at the project root — never invoke a bare `mvn`
from a system install.

- Find project root by walking up from your worktree to the directory containing `.kiln/`.
- **On first startup**: if `./mvnw` (or `mvnw.cmd` on Windows) is missing at project root,
  generate it: `mvn wrapper:wrapper -Dmaven=3.9.9` (requires a system Maven available once,
  only to bootstrap the wrapper).
- **Always invoke Maven via the wrapper**: `./mvnw <goal>` (Unix) / `.\mvnw.cmd <goal>`
  (Windows) — never a bare `mvn` command.
- Verify JDK 21+ is on `PATH` (`java -version`) before running any Maven goal.
- **Do NOT create a second Maven wrapper or root `pom.xml`** — the multi-module build is defined
  once at project root.

## Package Layout

Two Maven modules, one per bounded context, under a parent POM, each with a standard `src/` tree
— no flat package-at-root layout:

```
pom.xml                               ← parent POM, packaging: pom, <modules>
catalog-service/                      ← Maven module
  pom.xml                             ← <parent> points at root pom.xml
  src/main/java/com/libraryhub/catalog/
    domain/         ← records/final classes, port interfaces, no Spring/JPA/Jackson imports
    application/    ← use cases; imports domain only, never infrastructure
    infrastructure/ ← Spring MVC controllers, Spring Data JPA repositories, Spring AMQP adapters
loans-service/                        ← same structure; plural naming matches the README section
                                          heading and this module's name everywhere
```

The project root holds no business logic — it is orchestration and configuration only:
parent `pom.xml`, `mvnw`/`mvnw.cmd`, `.mvn/`.

Dependency direction: `infrastructure` → `application` → `domain`. Never the reverse.
Domain classes are pure Java records/final classes — no `@Entity`, `@Table`, `@JsonProperty`, or
any other Spring/JPA/Jackson annotation allowed. JPA entities live only in
`infrastructure/persistence/` and are mapped to/from domain objects at the repository adapter.

**The bounded contexts are independent services.** They share no code and no database; they
communicate only by publishing and consuming events. ArchUnit must enforce both halves of that —
the layering *within* each context and the isolation *between* them. Six rules, not four:

```java
@AnalyzeClasses(packages = "com.libraryhub", importOptions = ImportOption.DoNotIncludeTests.class)
class ArchitectureTest {

    @ArchTest
    static final ArchRule catalogDomainIsIndependent =
        noClasses().that().resideInAPackage("..catalog.domain..")
            .should().dependOnClassesThat()
            .resideInAnyPackage("..catalog.application..", "..catalog.infrastructure..");

    @ArchTest
    static final ArchRule catalogApplicationAvoidsInfrastructure =
        noClasses().that().resideInAPackage("..catalog.application..")
            .should().dependOnClassesThat().resideInAPackage("..catalog.infrastructure..");

    // ...the same two for loans, plus both directions of context isolation:

    @ArchTest
    static final ArchRule catalogDoesNotDependOnLoans =
        noClasses().that().resideInAPackage("..catalog..")
            .should().dependOnClassesThat().resideInAPackage("..loans..");

    @ArchTest
    static final ArchRule loansDoesNotDependOnCatalog =
        noClasses().that().resideInAPackage("..loans..")
            .should().dependOnClassesThat().resideInAPackage("..catalog..");
}
```

Without the last two nothing stops one context importing the other's domain directly, which is
the failure the whole architecture exists to prevent. Maven's module boundaries do not enforce
this on their own — a `<dependency>` added between the two service modules would make it compile.

## Test Layout

All tests live under each module's own `src/test/java/com/libraryhub/<context>/`:

```
src/test/java/com/libraryhub/catalog/
  unit/
    domain/         ← unit tests for catalog domain (pure Java, no I/O)
    application/    ← unit tests for catalog application services (Mockito-mocked ports)
    infrastructure/
  acceptance/
    CucumberTest.java   ← JUnit 5 suite runner (@IncludeEngines("cucumber"))
    steps/
      CatalogSteps.java ← Cucumber-JVM step defs for features/cat-*.feature
  property/           ← jqwik property-based tests (see /property-test-generator skill)
    domain/           ← invariant tests for domain entities/value objects
    application/

src/test/resources/features/   ← Gherkin specs (do not modify; owned by specifier)
```

Same structure repeats under `loans-service/src/test/java/com/libraryhub/loans/`.

## Testing Rules

- **Unit tests** (`unit/`): pure Java, Mockito-mock all ports (repositories, publishers), no I/O, no DB.
- **Acceptance tests** (`acceptance/steps/`): Cucumber-JVM step definitions that execute the `.feature` files. Use Testcontainers (`postgresql`, `rabbitmq` modules) for PostgreSQL and RabbitMQ — do NOT use an embedded/in-memory database as a substitute for Testcontainers in acceptance tests.
- **Every feature file must have a wired runner.** `CucumberTest.java` must exist per module with `@IncludeEngines("cucumber")` and point at that module's `src/test/resources/features/` — a feature file with no runner picking it up leaves it as dead documentation.
- **One PostgreSQL container per bounded context.** The catalog's and the loans' acceptance
  suites each provision their own `PostgreSQLContainer`, not one shared instance both
  `DataSource`s point at. The contexts are independent services that must be able to run against
  independent databases, and a single shared container means the suite never demonstrates that.
  Separate JPA entity sets are not a substitute: they hide a cross-context foreign key or a table
  name collision instead of failing on it.
- **A cross-context event must be tested across the contexts, in one scenario.** At least one
  acceptance scenario has to publish from the producing context and observe the effect in the
  consuming one — for `BookReturned`: loans returns a book, and the catalog's stock goes up. Both
  applications connect to the *same* `RabbitMQContainer` so the event actually travels.
  - Testing the two halves separately does not count. A loans test asserting against a recording
    publisher, plus a catalog test that hand-builds a message and invokes the listener directly,
    leaves the join between them — the part most likely to be wrong — untested by both.
  - A publisher that accepts and drops the event is not acceptable as the producing half. With a
    no-op publisher on one side the scenario proves nothing about the path.
  - Wait for the consumer to be bound before publishing, and poll for the effect rather than
    sleeping: an AMQP listener that has not finished binding silently discards the first message.
- **Seed the catalog from a single `SeedBooks` fixture, never from repeated literals.** Create
  `catalog-service/src/test/java/com/libraryhub/catalog/SeedBooks.java` holding the three books
  from the requirements and reference it everywhere. They are pinned because their titles,
  authors and genres are chosen so a filter term matches at most one of them; retyping the values
  breaks that quietly, and the breakage surfaces only as an acceptance failure the coder cannot
  fix. A feature file's `Background` table must match the fixture exactly. The scaffolded
  `GateConfigTest` asserts the fixture still holds the pinned values.
- **Prohibited patterns**:
  - Single giant `AllTests.java` files that bypass Maven's normal test discovery (Surefire/Failsafe)
  - An embedded/in-memory database as a substitute for Testcontainers in acceptance tests
  - A step class with `@Given`/`@When`/`@Then` methods but no `CucumberTest` runner wiring it up
  - One database container shared between both bounded contexts
  - A no-op publisher standing in for the producing half of a cross-context event test

## pom.xml Requirements

Each service module's `pom.xml` must declare (versions pinned in the parent `pom.xml`'s
`<dependencyManagement>`, not repeated per module):

```xml
<dependencies>
    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-data-jpa</artifactId>
    </dependency>
    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-amqp</artifactId>
    </dependency>
    <dependency>
        <groupId>org.postgresql</groupId>
        <artifactId>postgresql</artifactId>
        <scope>runtime</scope>
    </dependency>

    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-test</artifactId>
        <scope>test</scope>
    </dependency>
    <dependency>
        <groupId>io.cucumber</groupId>
        <artifactId>cucumber-java</artifactId>
        <scope>test</scope>
    </dependency>
    <dependency>
        <groupId>io.cucumber</groupId>
        <artifactId>cucumber-junit-platform-engine</artifactId>
        <scope>test</scope>
    </dependency>
    <dependency>
        <groupId>org.testcontainers</groupId>
        <artifactId>postgresql</artifactId>
        <scope>test</scope>
    </dependency>
    <dependency>
        <groupId>org.testcontainers</groupId>
        <artifactId>rabbitmq</artifactId>
        <scope>test</scope>
    </dependency>
    <dependency>
        <groupId>net.jqwik</groupId>
        <artifactId>jqwik</artifactId>
        <scope>test</scope>
    </dependency>
</dependencies>
```

Maven plugins required in the parent POM's `<pluginManagement>`: `spring-boot-maven-plugin`,
`jacoco-maven-plugin`, `pitest-maven` (`org.pitest:pitest-maven`), `maven-checkstyle-plugin` (or
`spotless-maven-plugin`), `spotbugs-maven-plugin`, `dependency-check-maven`.

### Reports go to one place

A multi-module reactor writes each module's reports under its own `target/`, which leaves the
project with no single answer to "how many tests are there". Point Surefire and Failsafe at a
reactor-level directory in the parent POM so every module appends to the same place:

```xml
<properties>
  <aggregate.reports>${session.executionRootDirectory}/target/test-reports</aggregate.reports>
</properties>
...
<plugin>
  <artifactId>maven-surefire-plugin</artifactId>
  <configuration><reportsDirectory>${aggregate.reports}</reportsDirectory></configuration>
</plugin>
```

Delete that directory before a full run — stale XML from a deleted test class is counted as a
passing test forever otherwise.

## Local Run

To start each service locally for manual testing or development:

```bash
# Catalog service (default port 8000)
./mvnw -pl catalog-service spring-boot:run

# Loans service (alternate port to avoid conflict)
./mvnw -pl loans-service spring-boot:run -Dspring-boot.run.arguments=--server.port=8001
```

## Quality Gates

Coverage, style, and layering are gated on every handoff, including the coder's. Mutation
testing (PIT) and CRAP are the architect's/refactorer's responsibility, not the coder's (see
`constitution/roles/coder.md` and `refactorer.md` → Non-Ownership):

- **Mutation score ≥ 80%** on `domain/` and `application/`:
  `./mvnw org.pitest:pitest-maven:mutationCoverage`

  Configure the threshold in the POM so the goal *fails* rather than merely reporting:

  ```xml
  <configuration>
    <targetClasses>
      <param>com.libraryhub.*.domain.*</param>
      <param>com.libraryhub.*.application.*</param>
    </targetClasses>
    <mutationThreshold>80</mutationThreshold>   <!-- without this PIT exits 0 at any score -->
    <timestampedReports>false</timestampedReports>
  </configuration>
  ```

  Delete `target/pit-reports` before a re-run: a stale history file makes PIT skip mutants it
  believes are already covered, and the score comes back green without them.

  Record each run's score in `.mutation-scores.json` (see below). The threshold is a floor, not
  a target: a score that falls while staying above 80% is a regression and must be reported.

  A per-cycle scoped configuration (a POM profile narrowing `<targetClasses>` to the changed
  packages) is encouraged and **must be committed** if its score is quoted in a handoff. A number
  nobody else can re-run is not evidence.

- **Coverage ≥ 90%**: `./mvnw verify` with `jacoco:check` bound to the build.

  ```xml
  <execution>
    <id>jacoco-check</id>
    <goals><goal>check</goal></goals>
    <configuration>
      <rules><rule>
        <element>BUNDLE</element>
        <limits><limit>
          <counter>LINE</counter><value>COVEREDRATIO</value><minimum>0.90</minimum>
        </limit></limits>
      </rule></rules>
    </configuration>
  </execution>
  ```

  **`jacoco:report` never fails a build — only `jacoco:check` does.** A threshold configured on
  the `report` execution parses fine and is silently ignored, so the gate reports green at any
  coverage. Bind `check` to `verify` and confirm it runs; a build log with no
  `jacoco-check` line is a build with no coverage gate.

  **Do not add `<excludes>` to the JaCoCo configuration.** The acceptance suite runs against real
  containers and covers the infrastructure adapters, so they need no exemption; an exclusion
  removes a class from the denominator rather than testing it. If a class genuinely cannot be
  covered, say so in the handoff instead.

- **CRAP ≤ 30 per method** (PIT's CRAP metric — differs from the Python example's radon threshold
  of ≤6, see the `crap-analyzer` skill's "Threshold Note")

- **Style**: `./mvnw checkstyle:check spotless:check`

  The Checkstyle ruleset must be committed at `config/checkstyle/checkstyle.xml` and referenced
  from the POM — never left to the plugin's default. The bundled `sun_checks.xml` and
  `google_checks.xml` differ substantially in what they catch, and a project with no explicit
  `<configLocation>` silently gets whichever the plugin version defaults to. Set
  `<violationSeverity>warning</violationSeverity>` so warnings fail the build; the default of
  `error` lets every warning-level rule pass unnoticed. Do not narrow the ruleset to make a
  build pass — disable a specific rule with a documented reason instead.

- **SAST**: `./mvnw spotbugs:check` with the `findsecbugs` plugin, `<threshold>Medium</threshold>`.

- **Dependency audit**: `./mvnw dependency-check:check`.

- **Layering**: the six ArchUnit rules above pass as part of `./mvnw test`. ArchUnit failures are
  test failures, so they surface through Surefire rather than as a separate gate — a module whose
  `ArchitectureTest` class is missing has no layering gate at all, and nothing reports that.

### `.mutation-scores.json`

Committed at the project root; the architect updates it every cycle:

```json
{
  "version": 1,
  "updated": "YYYY-MM-DD",
  "threshold": 80.0,
  "scores": {
    "catalog-service": {"last_score": 0.0, "mutants": 0, "survivors": 0,
                        "known_equivalent": 0, "known_equivalent_detail": "", "last_run": ""},
    "loans-service":   {"last_score": 0.0, "mutants": 0, "survivors": 0,
                        "known_equivalent": 0, "known_equivalent_detail": "", "last_run": ""}
  }
}
```

`known_equivalent` and its detail field are where equivalent mutants are accounted for — in the
record, and in PIT's `<excludedMethods>`/`<avoidCallsTo>` configuration, never by editing the
production code they land on (see `engineering.md`, "A gate measures the code").
