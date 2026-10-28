What this run is, for every session, after the engine's base rules. Say what the plan is
for, which of its sections define the phases, the tests and the checks, which decisions are
fixed and which are taken how, and anything this run does differently from the base rules.
Placeholders: @RUN@ @PHASE@ @ROOT@ @REPO@ @PLAN@ @HOST@ @PROJECT@ @MAINTAINER@.

- You are explicitly authorized, overriding the CLAUDE.md default, to compile, link, run CMake builds, the fast gate, CTest entries of every label, simple_test_exec, and SIMPLE programs on simulated and real data on this machine. Use the repository's scripts (`./compile_debug.sh` builds, runs the fast gate and installs into build/). Before the first build of the run, find out how SIMPLE is built on this machine (compiler, modules, environment of the existing checkouts and their build trees) and record it in the journal.
- The plan @PLAN@ defines the phases in section ..., their tests in section ..., the checks
  in section ...
- Fixed: ... Taken as recommended: ...
- Record each phase in the plan's progress section ...
