import pytest

from tests.test_hostile_10m import NODE_CLONE, load_inventory, moved_world

JOB = "jobs:\n  b:\n    runs-on: ubuntu-latest\n"
ARCH_MATRIX = JOB + "    strategy:\n      matrix:\n        arch: [amd64, riscv64]\n"


@pytest.mark.parametrize(
    "text",
    [
        JOB + "    env: {GOARCH: riscv64}\n    steps:\n      - run: go build\n",
        JOB + "    env: {GOOS: linux, GOARCH: riscv64}\n",
        JOB + '    env:\n      "GOARCH": riscv64\n',
        JOB + "    env:\n      'GOARCH': riscv64\n",
        JOB + "    env: {\"GOARCH\": 'riscv64', GOOS: linux}\n",
    ],
    ids=["flow-env", "flow-env-second-key", "quoted-key-double", "quoted-key-single", "flow-env-quoted-key"],
)
def test_10o_goarch_in_a_flow_mapping_or_under_a_quoted_key_is_read(text):
    assert load_inventory().workflow_linux_arches(text) == frozenset({"riscv64"})


@pytest.mark.parametrize(
    "text",
    [
        JOB + "    env: {GOARCH: [riscv64]}\n",
        JOB + "    env: {GOARCH: ${{ inputs.arch }}}\n",
        JOB + "    env: {GOARCH: riscv64 amd64}\n",
    ],
    ids=["flow-list", "flow-expression", "flow-two-words"],
)
def test_10o_flow_mapping_the_parser_cannot_read_fails_closed(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.workflow_linux_arches(text)


@pytest.mark.parametrize(
    ("text", "arches"),
    [
        (ARCH_MATRIX + "    steps:\n      - run: make build ARCH=${{ matrix.arch }}\n", {"amd64", "riscv64"}),
        (ARCH_MATRIX + "    steps:\n      - run: ./scripts/build.sh linux ${{ matrix.arch }}\n", {"amd64", "riscv64"}),
        (ARCH_MATRIX + "    steps:\n      - run: go test ./...\n", {"amd64", "riscv64"}),
        (
            JOB + "    strategy:\n      matrix:\n        os: [ubuntu-latest]\n        include:\n          - arch: riscv64\n"
            "    steps:\n      - run: make ARCH=${{ matrix.arch }}\n",
            {"riscv64"},
        ),
        (
            JOB + "    strategy:\n      matrix:\n        platform: [linux/amd64, linux/riscv64]\n",
            {"amd64", "riscv64"},
        ),
        (
            JOB + "    strategy:\n      matrix:\n        GOOS: [linux]\n        GOARCH: [riscv64]\n"
            "    env:\n      GOARCH: ${{ matrix.GOARCH }}\n",
            {"riscv64"},
        ),
        (
            JOB + "    strategy:\n      matrix:\n        GOOS: [linux]\n        GOARCH: [riscv64]\n"
            "    env:\n      GOARCH: ${{ matrix.goarch }}\n",
            {"riscv64"},
        ),
    ],
    ids=[
        "arch-key-make",
        "arch-key-script",
        "arch-key-unreferenced",
        "arch-key-in-include",
        "platform-key",
        "upper-keys-upper-reference",
        "upper-keys-lower-reference",
    ],
)
def test_10o_any_matrix_key_holding_go_arch_names_counts_them_as_shipped(text, arches):
    assert load_inventory().workflow_linux_arches(text) == frozenset(arches)


@pytest.mark.parametrize(
    "text",
    [
        JOB + '    strategy:\n      matrix:\n        target: ["GOARCH=riscv64"]\n'
        "    steps:\n      - run: env ${{ matrix.target }} go build\n",
        JOB + "    strategy:\n      matrix:\n        cfg: [fast, slow]\n    steps:\n      - run: make TARGET=${{ matrix.cfg }}\n",
        JOB + "    strategy:\n      matrix:\n        cfg: [fast, slow]\n    steps:\n      - run: make PLATFORM=${{ matrix.cfg }}\n",
        JOB + "    steps:\n      - run: make build ARCH=${{ matrix.arch }}\n",
        JOB + "    strategy:\n      matrix:\n        arch: ${{ fromJSON(needs.a.outputs.arches) }}\n"
        "    steps:\n      - run: make ARCH=${{ matrix.arch }}\n",
        JOB + "    strategy:\n      matrix:\n        arch: [amd64, riscv64]\n    env:\n      GOARCH: ${{ matrix.arch }}\n",
    ],
    ids=[
        "target-assignment",
        "target-word",
        "platform-word",
        "no-matrix",
        "expression-values",
        "goarch-from-arch-key",
    ],
)
def test_10o_matrix_key_used_beside_an_arch_word_without_an_arch_list_fails_closed(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.workflow_linux_arches(text)


@pytest.mark.parametrize(
    "text",
    [
        JOB + "    steps:\n      - run: |\n          for GOARCH in amd64 riscv64; do\n            export GOARCH\n            go build\n          done\n",
        JOB + "    steps:\n      - run: for GOARCH in amd64 riscv64; do export GOARCH; go build; done\n",
        JOB + "    steps:\n      - run: |\n          read -r GOARCH < arch.txt\n          go build\n",
        JOB + '    steps:\n      - run: GOARCH="riscv"64 go build\n',
        JOB + "    steps:\n      - run: GOARCH='riscv'\"64\" go build\n",
    ],
    ids=["for-loop-export", "for-loop-one-line", "read-builtin", "split-double-quote", "split-mixed-quote"],
)
def test_10o_shell_spellings_the_parser_cannot_read_fail_closed(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.workflow_linux_arches(text)


@pytest.mark.parametrize(
    ("text", "arches"),
    [
        (JOB + '    env:\n      GOARCH: amd64\n    steps:\n      - run: echo "GOARCH=$GOARCH"\n', {"amd64"}),
        (JOB + '    env:\n      GOARCH: amd64\n    steps:\n      - run: echo "GOARCH=${GOARCH}" >> "$GITHUB_ENV"\n', {"amd64"}),
        (
            JOB + "    steps:\n      - run: |\n          docker buildx build --platform \\\n"
            "            linux/amd64,linux/riscv64 \\\n            --push .\n",
            {"amd64", "riscv64"},
        ),
        (JOB + '    steps:\n      - run: docker buildx build --platform "linux/amd64, linux/riscv64" .\n', {"amd64", "riscv64"}),
        (JOB + "    steps:\n      - uses: docker/setup-qemu-action@v3\n        with:\n          platforms: all\n", set()),
        (
            JOB + "    steps:\n      - name: emulators\n        uses: docker/setup-qemu-action@v3\n"
            "        with:\n          platforms: arm64,riscv64\n",
            set(),
        ),
        (
            JOB + "    steps:\n      - uses: docker/build-push-action@v7\n        with:\n          platforms: |\n"
            "            linux/amd64\n            linux/riscv64\n",
            {"amd64", "riscv64"},
        ),
        (JOB + "    env:\n      DOCKER_DEFAULT_PLATFORM: linux/riscv64\n", {"riscv64"}),
    ],
    ids=[
        "echo-self-reference",
        "echo-braced-self-reference",
        "platform-continuation",
        "platform-quoted-space",
        "qemu-all",
        "qemu-arch-list",
        "platforms-block-scalar",
        "docker-default-platform",
    ],
)
def test_10o_realistic_lines_are_read_and_emulator_setup_is_not_a_build_line(text, arches):
    assert load_inventory().workflow_linux_arches(text) == frozenset(arches)


@pytest.mark.parametrize(
    ("text", "arches"),
    [
        ("FROM --platform=$BUILDPLATFORM golang:1 AS b\nARG TARGETARCH\nRUN GOOS=${TARGETOS} GOARCH=${TARGETARCH} make build\n", set()),
        ("FROM golang:1\nRUN CGO_ENABLED=0 GOARCH=riscv64 go build \\\n    -o /n .\n", {"riscv64"}),
        ("FROM --platform=linux/riscv64 alpine:3\n", {"riscv64"}),
        ("NAME = node-$(GOOS)-$(GOARCH)\nGOARCH ?= riscv64\nbuild:\n\tgo build -o $(NAME)\n", {"riscv64"}),
        ("export GOARCH := s390x\n", {"s390x"}),
        ("build:\n\tGOARCH=$(GOARCH) go build\n", set()),
    ],
    ids=["docker-target-arg", "docker-literal", "docker-from-platform", "make-default", "make-export", "make-self"],
)
def test_10o_dockerfile_and_makefile_build_lines_are_read(text, arches):
    assert load_inventory().build_file_linux_arches(text) == arches


@pytest.mark.parametrize(
    "text",
    [
        "GOARCH ?= $(shell uname -m)\n",
        "RUN GOARCH=$ARCH go build\n",
        "RUN docker buildx bake\n",
        "release:\n\tgoreleaser release --clean\n",
    ],
    ids=["make-shell", "docker-other-arg", "bake", "goreleaser"],
)
def test_10o_build_file_lines_the_parser_cannot_read_fail_closed(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.build_file_linux_arches(text)


@pytest.mark.parametrize(
    "extra",
    [
        {".goreleaser.yaml": "builds:\n  - goos: [linux]\n    goarch: [riscv64]\n"},
        {"build/goreleaser.yml": "builds:\n  - goarch: [riscv64]\n"},
        {"docker-bake.hcl": 'target "n" {\n  platforms = ["linux/riscv64"]\n}\n'},
        {".github/workflows/r.yml": JOB + "    steps:\n      - run: docker buildx bake --set '*.platform=linux/riscv64'\n"},
        {".github/workflows/r.yml": JOB + "    steps:\n      - uses: docker/bake-action@v5\n"},
        {".github/workflows/r.yml": JOB + "    steps:\n      - uses: goreleaser/goreleaser-action@v6\n"},
        {".github/workflows/r.yml": JOB + '    steps:\n      - run: gox -osarch="linux/riscv64"\n'},
        {".github/workflows/r.yml": JOB + "    steps:\n      - run: xgo --targets=linux/riscv64 .\n"},
    ],
    ids=[
        "goreleaser-root",
        "goreleaser-nested",
        "bake-file",
        "bake-run",
        "bake-action",
        "goreleaser-action",
        "gox",
        "xgo",
    ],
)
def test_10o_scope_gate_reads_workflows_dockerfile_and_makefile_and_refuses_goreleaser_bake_and_gox(tmp_path, extra):
    result = moved_world(tmp_path, "c.go", "", extra)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "shipped linux arch set" in result.stdout, result.stdout


@pytest.mark.parametrize(
    "extra",
    [
        {".github/workflows/r.yml": JOB + "    env: {GOARCH: riscv64}\n    steps:\n      - run: go build\n"},
        {".github/workflows/r.yml": JOB + '    env:\n      "GOARCH": riscv64\n'},
        {".github/workflows/r.yml": ARCH_MATRIX + "    steps:\n      - run: make build ARCH=${{ matrix.arch }}\n"},
        {
            ".github/workflows/r.yml": JOB + "    strategy:\n      matrix:\n        GOOS: [linux]\n        GOARCH: [riscv64]\n"
            "    env:\n      GOARCH: ${{ matrix.goarch }}\n"
        },
        {"Makefile": "GOARCH ?= riscv64\nbuild:\n\tgo build\n"},
        {"Dockerfile": "FROM golang:1\nRUN GOARCH=riscv64 go build .\n"},
        {".github/actions/build/action.yml": "runs:\n  using: composite\n  steps:\n    - run: GOARCH=riscv64 go build\n"},
    ],
    ids=["flow-env", "quoted-key", "matrix-arch-make", "upper-matrix-keys", "makefile", "dockerfile", "composite-action"],
)
def test_10o_riscv64_shipped_through_these_sources_is_honoured_at_gate_time(tmp_path, extra):
    result = moved_world(tmp_path, "c.go", "//go:build !riscv64\n\n", extra)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "excluded from the build" in result.stdout, result.stdout


@pytest.mark.parametrize(
    "extra",
    [
        {".github/workflows/r.yml": JOB + "    steps:\n      - run: for GOARCH in amd64 riscv64; do export GOARCH; go build; done\n"},
        {".github/workflows/r.yml": JOB + '    steps:\n      - run: GOARCH="riscv"64 go build\n'},
        {
            ".github/workflows/r.yml": JOB + '    strategy:\n      matrix:\n        target: ["GOARCH=riscv64"]\n'
            "    steps:\n      - run: env ${{ matrix.target }} go build\n"
        },
    ],
    ids=["for-loop-export", "split-quote", "matrix-target-env"],
)
def test_10o_unreadable_shell_or_matrix_source_fails_closed_at_gate_time(tmp_path, extra):
    result = moved_world(tmp_path, "c.go", "", extra)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "shipped linux arch set" in result.stdout, result.stdout


def test_10o_real_node_shaped_dockerfile_and_makefile_do_not_widen_the_set(tmp_path):
    extra = {
        "Dockerfile": "FROM --platform=$BUILDPLATFORM golang:1 AS b\nARG TARGETOS\nARG TARGETARCH\n"
        "RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} make NAME=main build\n",
        "Makefile": "NAME = node-$(GOOS)-$(GOARCH)\nXRAY_ARCH_MAP_riscv64 = riscv64\n"
        "XRAY_ARCH_EFFECTIVE := $(XRAY_ARCH_MAP_$(GOARCH))\n",
    }
    result = moved_world(tmp_path, "c.go", "//go:build !riscv64\n\n", extra)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("ref", ["86da26e5", "2d8e11f3", "0544e60b"])
def test_10o_real_node_workflows_and_build_files_read_as_the_shipped_set(monkeypatch, ref):
    if not (NODE_CLONE / ".git").exists():
        pytest.skip(f"node clone not present at {NODE_CLONE}")
    module = load_inventory()
    monkeypatch.chdir(NODE_CLONE)
    assert module.shipped_linux_arches(ref) == module.GO_SHIPPED_ARCHES
