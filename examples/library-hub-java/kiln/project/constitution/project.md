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
- **Prohibited patterns**:
  - Single giant `AllTests.java` files that bypass Maven's normal test discovery (Surefire/Failsafe)
  - An embedded/in-memory database as a substitute for Testcontainers in acceptance tests
  - A step class with `@Given`/`@When`/`@Then` methods but no `CucumberTest` runner wiring it up

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
`spotless-maven-plugin`).

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

- Mutation score ≥ 80% on `domain/` and `application/`: `./mvnw org.pitest:pitest-maven:mutationCoverage`
- Coverage ≥ 90%: `./mvnw jacoco:report jacoco:check`
- CRAP ≤ 30 per method (PIT's CRAP metric — differs from the Python example's radon threshold of ≤6, see the `crap-analyzer` skill's "Threshold Note")
- Style: `./mvnw checkstyle:check spotless:check`
- Layering: ArchUnit layer-dependency test passes as part of `./mvnw test`
