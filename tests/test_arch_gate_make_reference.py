import pytest

from tests.test_hostile_10m import load_inventory

MAKE_GUARD = (
    'echo "Skipping the geo files check: cross build for $(GOOS)/$(GOARCH), '
    '$(GEOFILES_XRAY) cannot run on this host"'
)
MAKE_COMPARISON = 'if { [ -n "$(GOARCH)" ] && [ "$(GOARCH)" != "$$host_arch" ]; }; then :; fi'
MAKE_BOTH = (
    'if { [ -n "$(GOOS)" ] && [ "$(GOOS)" != "$$host_os" ]; } || '
    '{ [ -n "$(GOARCH)" ] && [ "$(GOARCH)" != "$$host_arch" ]; }; then '
    'echo "Skipping the geo files check: cross build for $(GOOS)/$(GOARCH)"; exit 0; fi'
)


@pytest.mark.parametrize(
    "line",
    [MAKE_GUARD, MAKE_COMPARISON, MAKE_BOTH],
    ids=["message-naming-a-cross-build", "comparison-guard", "guard-and-message-together"],
)
def test_a_make_expansion_of_goarch_is_a_reference_not_a_binding(line):
    load_inventory().refuse_shell_name_forms(line)


@pytest.mark.parametrize(
    "line",
    [
        "for goarch in amd64 arm64; do go build; done",
        "read goarch rest < arches.txt",
        "export goarch",
        "declare goarch",
        "printf -v goarch %s amd64",
        "for targetarch in amd64; do :; done",
        "for p in $(go env GOARCH); do :; done",
    ],
    ids=["for", "read", "export", "declare", "printf-v", "targetarch", "go-env-at-build-time"],
)
def test_a_bare_goarch_binding_is_still_refused(line):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.refuse_shell_name_forms(line)


def test_an_ordinary_assignment_stays_readable():
    load_inventory().refuse_shell_name_forms("export GOARCH=amd64")
