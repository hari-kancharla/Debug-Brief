{
  pkgs ? import <nixpkgs> { },
}:

let
  python = pkgs.python312;
  pyPkgs = python.pkgs;
in
pyPkgs.buildPythonApplication rec {
  pname = "debugbrief";
  version =
    let
      pyproject = builtins.readFile ./pyproject.toml;
      # find the line `version = "..."`
      # and extract the value between
      # quotes
      versionLine = builtins.filter (line: builtins.match "version = \"[^\"]+\"" line != null) (
        builtins.split "\n" pyproject
      );
      # now extract the actual version string from the line
      versionString =
        let
          line = builtins.head versionLine;
          parts = builtins.split "\"" line;
        in
        parts [ 1 ];
    in
    versionString;

  src = ./.;
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
  ];

  checkPhase = ''
    runHook preCheck
    pytest -q -k "not runner_robustness"
    runHook postCheck
  '';

  meta = with pkgs.lib; {
    description = "Local-first CLI that turns a debugging session into an honest markdown brief for PRs, handoffs and incidents";
    homepage = "https://github.com/harihkk/Debug-Brief";
    license = licenses.mit;
    mainProgram = "debugbrief";
    platforms = platforms.unix;
  };
}
