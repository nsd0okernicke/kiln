# Property tests

This suite checks invariants over generated inputs. Its directory structure mirrors
`src/kiln`, just like the unit and integration suites.

Keep tests here when their value comes from exploring an input space with Hypothesis.
Place fixed examples and regressions in `tests/unit`, even when they exercise the same
production module.

Name modules `test_<subject>_properties.py` so pytest can collect them together with
same-subject modules in the mirrored unit and integration suites without import collisions.
