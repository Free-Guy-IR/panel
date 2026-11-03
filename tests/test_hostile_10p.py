import pytest

from tests.test_hostile_10m import NODE_CLONE, load_inventory, moved_world

JOB = "jobs:\n  b:\n    runs-on: ubuntu-latest\n"


@pytest.mark.parametrize(
    ("text", "arches"),
    [
        ("FROM golang:1\nENV GOARCH riscv64\nRUN go build .\n", {"riscv64"}),
        ("FROM golang:1\nenv GOARCH riscv64\nRUN go build .\n", {"riscv64"}),
        ('FROM golang:1\nENV GOARCH "riscv64"\n', {"riscv64"}),
        ("FROM golang:1\nARG TARGETARCH\nENV GOARCH $TARGETARCH\n", set()),
        ("FROM golang:1\nENV GOOS linux\n", set()),
    ],
    ids=["env-legacy", "env-legacy-lowercase", "env-legacy-quoted", "env-legacy-target-arg", "env-legacy-other-key"],
)
def test_10p_dockerfile_legacy_env_form_is_read(text, arches):
    assert load_inventory().build_file_linux_arches(text) == arches


@pytest.mark.parametrize(
    "text",
    [
        "FROM golang:1\nENV GOARCH riscv64 amd64\n",
        "FROM golang:1\nENV GOARCH $ARCH\n",
        "FROM golang:1\nARG TARGETARCH=riscv64\nRUN GOARCH=$TARGETARCH go build\n",
        "FROM golang:1\narg TARGETARCH=riscv64\n",
    ],
    ids=["env-legacy-two-words", "env-legacy-other-arg", "arg-targetarch-default", "arg-targetarch-default-lowercase"],
)
def test_10p_dockerfile_legacy_env_or_targetarch_default_the_parser_cannot_read_fails_closed(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.build_file_linux_arches(text)


@pytest.mark.parametrize(
    "name",
    ["Makefile", "GNUmakefile", "makefile"],
)
@pytest.mark.parametrize(
    "line",
    ["include build/release.mk\n", "-include build/release.mk\n", "sinclude build/release.mk\n"],
    ids=["include", "dash-include", "sinclude"],
)
def test_10p_root_makefile_include_fails_closed(name, line):
    module = load_inventory()
    reader = module.shipped_linux_arch_source(name)
    assert reader is not None
    with pytest.raises(module.ShippedArchesUnreadable):
        reader(line + "build:\n\tgo build\n")


@pytest.mark.parametrize("name", ["GNUmakefile", "makefile"])
def test_10p_gnumakefile_and_lowercase_makefile_are_read(name):
    reader = load_inventory().shipped_linux_arch_source(name)
    assert reader is not None
    assert reader("GOARCH ?= riscv64\nbuild:\n\tgo build\n") == {"riscv64"}


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("docker/Dockerfile", "FROM golang:1\nRUN GOARCH=riscv64 go build\n"),
        ("build/Dockerfile.riscv64", "FROM --platform=linux/riscv64 golang:1\n"),
        ("deploy/node.Dockerfile", "FROM golang:1\nENV GOARCH riscv64\n"),
        ("Containerfile", "FROM golang:1\nARG TARGETARCH=riscv64\n"),
        ("build/Containerfile", "FROM golang:1\nENV GOARCH=riscv64\n"),
        ("build/release.mk", "GOARCH ?= riscv64\n"),
        ("release.mk", "export GOARCH := $(ARCH)\n"),
        ("scripts/build.sh", "#!/bin/sh\nGOARCH=riscv64 go build\n"),
        ("scripts/release/build.sh", "#!/bin/sh\nfor GOARCH in amd64 riscv64; do export GOARCH; go build; done\n"),
        ("scripts/image.sh", "#!/bin/sh\ndocker buildx build --platform linux/amd64,linux/riscv64 .\n"),
        ("scripts/arch.sh", '#!/bin/sh\nGOARCH="$1" go build\n'),
        (".goreleaser.yml.tmpl", "builds:\n  - goarch: [riscv64]\n"),
        (".goreleaser-pro.yaml", "builds:\n  - goarch: [riscv64]\n"),
        ("build/.goreleaser.json", "{}\n"),
    ],
    ids=[
        "subfolder-dockerfile",
        "subfolder-dockerfile-suffix",
        "subfolder-dot-dockerfile",
        "root-containerfile-targetarch-default",
        "subfolder-containerfile",
        "mk-subfolder",
        "mk-root-other-var",
        "script-literal",
        "script-nested-loop",
        "script-platform",
        "script-positional",
        "goreleaser-tmpl",
        "goreleaser-dash",
        "goreleaser-json",
    ],
)
def test_10p_scope_decision_refuses_subfolder_dockerfiles_containerfiles_mk_scripts_and_goreleaser_naming_an_arch(
    path, text
):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        reader = module.shipped_linux_arch_source(path)
        if reader is None:
            raise AssertionError(f"{path} is neither read nor refused")
        reader(text)


@pytest.mark.parametrize(
    ("path", "text"),
    [
        (
            "docker/Dockerfile",
            "FROM --platform=$BUILDPLATFORM golang:1\nARG TARGETARCH\nRUN GOARCH=${TARGETARCH} go build\n",
        ),
        ("build/common.mk", "VERSION ?= 1.0\n"),
        ("scripts/drift.sh", '#!/bin/sh\nif (a !~ /^(CGO_ENABLED|GOOS|GOARCH|GOARM)$/) return "other"\n'),
        ("scripts/floor.sh", 'printf "FROM --platform=\\$BUILDPLATFORM golang:1\\n" >"$r/Dockerfile"\n'),
        ("scripts/lint.sh", "#!/bin/sh\ngo vet ./...\n"),
    ],
    ids=[
        "subfolder-dockerfile-passthrough",
        "mk-no-arch",
        "script-goarch-word",
        "script-builder-platform",
        "script-no-arch",
    ],
)
def test_10p_scope_decision_does_not_refuse_sources_that_set_no_arch_value(path, text):
    module = load_inventory()
    reader = module.shipped_linux_arch_source(path)
    assert reader is None or reader(text) == set()


@pytest.mark.parametrize(
    "text",
    [
        JOB + '    steps:\n      - run: "GO\\x41RCH=riscv64 go build"\n',
        JOB + '    env:\n      "GO\\x41RCH": riscv64\n',
        JOB
        + '    steps:\n      - uses: docker/build-push-action@v6\n        with:\n          "pl\\x61tforms": linux/riscv64\n',
        JOB + '    strategy:\n      matrix:\n        p: ["linux\\x2Friscv64"]\n',
        JOB + '    steps:\n      - run: "GO\\u0041RCH=riscv64 go build"\n',
    ],
    ids=["run-hex", "env-key-hex", "platforms-key-hex", "matrix-platform-hex", "run-unicode"],
)
def test_10p_yaml_escape_spelling_goarch_or_a_platform_fails_closed(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.workflow_linux_arches(text)


def test_10p_yaml_escape_that_names_no_arch_is_not_refused():
    text = JOB + '    steps:\n      - run: "printf \\x41"\n'
    assert load_inventory().workflow_linux_arches(text) == frozenset()


@pytest.mark.parametrize(
    ("text", "arches"),
    [
        (
            JOB + "    strategy:\n      matrix:\n        platform: [linux/amd64, linux/riscv64]\n"
            "    steps:\n      - uses: docker/build-push-action@v6\n        with:\n          platforms: ${{ matrix.platform }}\n",
            {"amd64", "riscv64"},
        ),
        (
            JOB + "    strategy:\n      matrix:\n        platform:\n          - linux/amd64\n          - linux/arm64\n"
            "    steps:\n      - uses: docker/build-push-action@v6\n        with:\n          platforms: \"${{ matrix['platform'] }}\"\n",
            {"amd64", "arm64"},
        ),
        (
            JOB + "    strategy:\n      matrix:\n        include:\n          - platform: linux/riscv64\n"
            "    steps:\n      - run: docker buildx build --platform ${{ matrix.platform }} .\n",
            {"riscv64"},
        ),
    ],
    ids=["flow-list", "block-list-bracket", "include-flag"],
)
def test_10p_platforms_from_a_matrix_of_platform_strings_is_read(text, arches):
    assert load_inventory().workflow_linux_arches(text) == arches


@pytest.mark.parametrize(
    "text",
    [
        JOB + "    strategy:\n      matrix:\n        platform: [amd64, riscv64]\n"
        "    steps:\n      - uses: docker/build-push-action@v6\n        with:\n          platforms: ${{ matrix.platform }}\n",
        JOB
        + "    steps:\n      - uses: docker/build-push-action@v6\n        with:\n          platforms: ${{ matrix.platform }}\n",
        JOB + "    strategy:\n      matrix:\n        platform: ${{ fromJSON(inputs.p) }}\n"
        "    steps:\n      - uses: docker/build-push-action@v6\n        with:\n          platforms: ${{ matrix.platform }}\n",
    ],
    ids=["bare-arch-items", "no-matrix", "matrix-from-json"],
)
def test_10p_platforms_from_a_matrix_the_parser_cannot_read_fails_closed(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.workflow_linux_arches(text)


@pytest.mark.parametrize(
    "extra",
    [
        {"Dockerfile": "FROM golang:1\nENV GOARCH riscv64\nRUN go build .\n"},
        {"Dockerfile": "FROM golang:1\nenv GOARCH riscv64\n"},
        {"GNUmakefile": "GOARCH ?= riscv64\n"},
        {"makefile": "GOARCH ?= riscv64\n"},
        {
            ".github/workflows/r.yml": JOB
            + "    strategy:\n      matrix:\n        platform: [linux/amd64, linux/riscv64]\n"
            "    steps:\n      - uses: docker/build-push-action@v6\n        with:\n          platforms: ${{ matrix.platform }}\n"
        },
    ],
    ids=[
        "dockerfile-env-legacy",
        "dockerfile-env-legacy-lowercase",
        "gnumakefile",
        "lowercase-makefile",
        "matrix-platforms",
    ],
)
def test_10p_riscv64_shipped_through_these_sources_is_honoured_at_gate_time(tmp_path, extra):
    result = moved_world(tmp_path, "c.go", "//go:build !riscv64\n\n", extra)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "excluded from the build" in result.stdout, result.stdout


@pytest.mark.parametrize(
    "extra",
    [
        {"Makefile": "include build/release.mk\n", "build/release.mk": "GOARCH := riscv64\n"},
        {"Makefile": "-include local.mk\nbuild:\n\tgo build\n"},
        {"docker/Dockerfile": "FROM golang:1\nRUN GOARCH=riscv64 go build\n"},
        {"Containerfile": "FROM golang:1\nARG TARGETARCH=riscv64\n"},
        {"scripts/build.sh": "#!/bin/sh\nGOARCH=riscv64 go build\n"},
        {".goreleaser.yml.tmpl": "builds:\n  - goarch: [riscv64]\n"},
        {"Dockerfile": "FROM golang:1\nARG TARGETARCH=riscv64\n"},
        {".github/workflows/r.yml": JOB + '    steps:\n      - run: "GO\\x41RCH=riscv64 go build"\n'},
    ],
    ids=[
        "makefile-include",
        "makefile-dash-include",
        "subfolder-dockerfile",
        "containerfile-targetarch-default",
        "script",
        "goreleaser-tmpl",
        "dockerfile-targetarch-default",
        "yaml-hex-escape",
    ],
)
def test_10p_unread_or_refused_sources_fail_closed_at_gate_time(tmp_path, extra):
    result = moved_world(tmp_path, "c.go", "", extra)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "shipped linux arch set" in result.stdout, result.stdout


@pytest.mark.parametrize("ref", ["86da26e5", "2d8e11f3", "0544e60b"])
def test_10p_real_node_still_reads_exactly_the_shipped_set(monkeypatch, ref):
    if not (NODE_CLONE / ".git").exists():
        pytest.skip(f"node clone not present at {NODE_CLONE}")
    module = load_inventory()
    module.shipped_linux_arches.cache_clear()
    monkeypatch.chdir(NODE_CLONE)
    assert module.shipped_linux_arches(ref) == module.GO_SHIPPED_ARCHES
