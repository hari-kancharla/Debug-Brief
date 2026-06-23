{
  description = "DebugBrief - a local-first CLI for honest debugging briefs";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };

        python = pkgs.python312;
        pyPkgs = python.pkgs;

        debugbrief = pyPkgs.buildPythonApplication rec {
          pname = "debugbrief";
          version =
            let
              pyproject = builtins.readFile ./pyproject.toml;
              # find the line `version = "..."`
              # and extract the value between
              # quotes
              versionLine = builtins.filter (line: builtins.match "version = \"[^\"]+\"" line != null) (
                builtins.split [ "\n" ] pyproject
              );
              # now extract the actual version string from the line
              versionString =
                let
                  line = builtins.head versionLine;
                  parts = builtins.split [ "\"" ] line;
                in
                parts [ 1 ];
            in
            versionString;
          src = self;
          pyproject = true;

          nativeBuildInputs = [
            pyPkgs.setuptools
            pyPkgs.wheel
          ];

          propagatedBuildInputs = pkgs.lib.optionals (pkgs.lib.versionOlder python.version "3.11") [
            pyPkgs.tomli
            pkgs.git
          ];

          nativeCheckInputs = [
            pyPkgs.pytest
            pkgs.git
            pkgs.procps
          ];

          # The command-runner robustness tests depend on pgrep/pkill and are
          # intentionally OS/process-tool specific. Keep the package build
          # focused on packaging correctness and the broader test suite.
          checkPhase = ''
            runHook preCheck
            pytest -q
            runHook postCheck
          '';

          meta = with pkgs.lib; {
            description = "Local-first CLI that turns a debugging session into an honest markdown brief for PRs, handoffs and incidents";
            homepage = "https://github.com/harihkk/Debug-Brief";
            license = licenses.mit;
            mainProgram = "debugbrief";
            platforms = platforms.unix;
          };
        };

        devShell = pkgs.mkShell {
          packages = [
            python
            pkgs.git
            pyPkgs.pytest
            pyPkgs.ruff
            pyPkgs.mypy
            pyPkgs.build
            pyPkgs.setuptools
            pyPkgs.wheel
          ]
          ++ pkgs.lib.optionals (pkgs.lib.versionOlder python.version "3.11") [
            pyPkgs.tomli
          ];

          shellHook = ''
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
            echo "DebugBrief dev shell"
            echo "Python: ${python.version}"
            echo "Run tests: python -m pytest"
            echo "Lint: python -m ruff check src tests"
            echo "Typecheck: python -m mypy src/debugbrief"
            echo "Build: python -m build"
          '';
        };
      in
      {
        packages.default = debugbrief;
        packages.debugbrief = debugbrief;

        apps.default = flake-utils.lib.mkApp {
          drv = debugbrief;
        };

        checks.default = debugbrief;

        devShells.default = devShell;

        formatter = pkgs.nixfmt;
      }
    );
}
