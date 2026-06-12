# Renders docs/demo.gif: a failing test, the fix, redo, and the brief.
# Needs charmbracelet/vhs and debugbrief on PATH. Run from the repo root:
#   vhs docs/demo.tape
# The hidden setup builds a scratch repo, so re-renders are deterministic.

Output docs/demo.gif

Set Shell bash
Set FontSize 15
Set Width 1050
Set Height 640
Set Padding 14
Set TypingSpeed 35ms
Set Framerate 24

Hide
Type "cd \"$(mktemp -d)\" && git init -q -b main . && git config user.email demo@example.com && git config user.name Demo && printf 'def add(a, b):\n    return a - b\n' > calc.py && printf 'from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n' > test_calc.py && git add . && git commit -qm 'initial code' && clear"
Enter
Show

Sleep 800ms
Type `debugbrief start "Fix add() returning wrong result"`
Enter
Sleep 2.2s

Type "debugbrief run -- python -m pytest -q test_calc.py"
Enter
Sleep 3.2s

Type "# fix: add() should add, not subtract"
Enter
Type "sed -i '' 's/a - b/a + b/' calc.py"
Enter
Sleep 600ms
Type "git --no-pager diff calc.py"
Enter
Sleep 2.8s

Type "debugbrief redo"
Enter
Sleep 3.2s

Type "debugbrief end --stdout | head -n 30"
Enter
Sleep 7s
