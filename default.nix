{
  pkgs ? import <nixpkgs> { },
}:

let
  python = pkgs.python312;
  pyPkgs = python.pkgs;
in
pyPkgs.buildPythonApplication rec {
  pname = "debugbrief";
  version = "1.3.0";

  src = ./.;
  pyproject = true;

  nativeBuildInputs = [
    pyPkgs.setuptools
    pyPkgs.wheel
    pkgs.git
  ];

  propagatedBuildInputs = pkgs.lib.optionals (pkgs.lib.versionOlder python.version "3.11") [
    pyPkgs.tomli
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
