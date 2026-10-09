"""Single entry point wrapping every V2 subcommand: testbench.py <command>
[args...]. docs/ARCHITECTURE.md decision 3 - "testbench.py record" with zero further
arguments produces a fully evaluable RAW file; "testbench.py analyze <file>"
evaluates it fully.

Deliberately thin: each subcommand's actual argument parsing and logic stays
in its own module (acquisition/recorder.py etc.) - this file only rewrites
sys.argv and calls that module's own main(), so a flag is ever defined once,
not duplicated here. "testbench.py <command> --help" therefore shows that
module's own, real argparse help, not a second hand-maintained copy of it.
"""
import importlib
import sys

_COMMANDS = {
    "record": ("acquisition.recorder", "Record one RAW file per arm cycle (no wizard)."),
    "session": ("acquisition.session", "Guided session: wizard, live dashboard, results."),
    "calibrate": ("acquisition.calibrate", "Multi-point load cell calibration."),
    "analyze": ("analysis.analyzer", "Turn one RAW file into one ANALYZED file."),
    "compare": ("comparison.compare", "Combine several ANALYZED sweep files into one COMPARISON file."),
    "plot": ("comparison.plotting", "Draw a PNG from one or more COMPARISON files (overlaid)."),
}


def _usage() -> str:
    lines = ["usage: testbench.py <command> [args...]", "", "commands:"]
    width = max(len(name) for name in _COMMANDS)
    for name, (_, description) in _COMMANDS.items():
        lines.append(f"  {name:<{width}}  {description}")
    lines.append("\nRun 'testbench.py <command> --help' for that command's own arguments.")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print(_usage(), file=sys.stderr)
        sys.exit(1)
    if sys.argv[1] in ("-h", "--help"):
        print(_usage())
        sys.exit(0)

    command = sys.argv[1]
    entry = _COMMANDS.get(command)
    if entry is None:
        print(f"Unknown command: {command!r}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        sys.exit(1)

    module_name, _ = entry
    module = importlib.import_module(module_name)
    # Rewrite argv so the target module's own argparse-based main() sees
    # exactly what it would parsing `python -m <module_name> ...` directly -
    # sys.argv[0] becomes the prog name argparse shows in usage/help.
    sys.argv = [f"testbench.py {command}"] + sys.argv[2:]
    module.main()


if __name__ == "__main__":
    main()
