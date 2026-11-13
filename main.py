#!/usr/bin/env python3
"""RevFUR - Furnace sequence disassembler and sequence-only patcher."""
import argparse
import json
import queue
import sys
import threading
from pathlib import Path

if sys.version_info < (3, 10):
    detected = ".".join(str(part) for part in sys.version_info[:3])
    raise SystemExit(
        "RevFUR requires Python 3.10 or newer; detected Python " + detected + ".\n"
        "Install Python 3.10+ and launch with 'py -3.10 main.py' or run_windows.bat."
    )

from revfur_core import export_sequence, apply_sequence, reverse_entire_sequence, analyze_fur_file


def launch_gui(initial_file=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        root = TkinterDnD.Tk()
        dnd_enabled = True
    except Exception:
        root = tk.Tk()
        dnd_enabled = False
        DND_FILES = None

    root.title("RevFUR - Furnace Sequence Reverser")
    root.geometry("760x690")
    root.minsize(670, 590)
    root.configure(padx=16, pady=14)

    operation = tk.StringVar(value="Reverse entire sequence")
    module_var = tk.StringVar(value="")
    sequence_var = tk.StringVar(value="")
    output_var = tk.StringVar(value="")
    loop_back_var = tk.BooleanVar(value=True)
    status_var = tk.StringVar(value="Ready. Select or drop a Furnace .fur module.")
    messages = queue.Queue()

    header = ttk.Frame(root)
    header.pack(fill="x")
    ttk.Label(header, text="RevFUR", font=("Segoe UI", 22, "bold")).pack(anchor="w")
    ttk.Label(header, text="Detect playback loops and song ends, reverse the reachable sequence, or export it for editing",
              foreground="#666666").pack(anchor="w", pady=(0, 12))

    drop = tk.Label(root, text="DROP A FURNACE .FUR MODULE HERE\n\nor drop a .furseq.json file when applying a sequence",
                    relief="groove", bd=2, bg="#f2f4f7", fg="#27364b",
                    font=("Segoe UI", 11, "bold"), height=4)
    drop.pack(fill="x", pady=(0, 12))

    action_box = ttk.LabelFrame(root, text="Operation", padding=10)
    action_box.pack(fill="x", pady=(0, 10))
    operation_combo = ttk.Combobox(action_box, textvariable=operation, state="readonly",
                                   values=["Reverse entire sequence", "Analyze playback loops and song end", "Extract sequence from FUR", "Apply edited sequence to FUR"])
    operation_combo.pack(fill="x")

    def add_file_row(parent, label, variable, command):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=4)
        ttk.Label(frame, text=label, width=24).pack(side="left")
        ttk.Entry(frame, textvariable=variable).pack(side="left", fill="x", expand=True)
        ttk.Button(frame, text="Browse...", command=command).pack(side="left", padx=(8, 0))
        return frame

    files_box = ttk.LabelFrame(root, text="Files", padding=10)
    files_box.pack(fill="x", pady=(0, 10))

    def browse_module():
        selected = filedialog.askopenfilename(title="Select Furnace module",
            filetypes=[("Furnace modules", "*.fur"), ("All files", "*.*")])
        if selected:
            set_module(selected)

    def browse_sequence():
        selected = filedialog.askopenfilename(title="Select RevFUR sequence file",
            filetypes=[("RevFUR sequence", "*.furseq.json"), ("JSON files", "*.json"), ("All files", "*.*")])
        if selected:
            sequence_var.set(selected)
            if operation.get().startswith("Apply") and not output_var.get():
                output_var.set(str(Path(module_var.get()).with_name(Path(module_var.get()).stem + "_revfur.fur")))

    module_row = add_file_row(files_box, "Furnace module (.fur)", module_var, browse_module)
    sequence_row = add_file_row(files_box, "Sequence (.furseq.json)", sequence_var, browse_sequence)

    def browse_output():
        writing_fur = not operation.get().startswith("Extract")
        ext = ".fur" if writing_fur else ".furseq.json"
        types = [("Furnace module", "*.fur")] if writing_fur else [("RevFUR sequence", "*.furseq.json")]
        selected = filedialog.asksaveasfilename(title="Choose output file", defaultextension=ext,
                                                filetypes=types)
        if selected:
            output_var.set(selected)

    output_row = add_file_row(files_box, "Output file", output_var, browse_output)

    def update_operation(*_):
        selected = operation.get()
        applying = selected.startswith("Apply")
        extracting = selected.startswith("Extract")
        reversing = selected.startswith("Reverse")
        analyzing = selected.startswith("Analyze")
        if applying:
            sequence_row.pack(fill="x", pady=4)
        else:
            sequence_row.pack_forget()
        if analyzing:
            output_row.pack_forget()
        else:
            output_row.pack(fill="x", pady=4)
        if reversing:
            loop_options.pack(fill="x", pady=(0, 8))
        else:
            loop_options.pack_forget()

        for child in output_row.winfo_children():
            if isinstance(child, ttk.Label):
                child.configure(text="Output sequence JSON" if extracting else "Output reversed Furnace (.fur)" if reversing else "Output Furnace (.fur)")
        if module_var.get() and not analyzing:
            source = Path(module_var.get())
            if extracting:
                default_out = source.with_suffix(".furseq.json")
            elif reversing:
                default_out = source.with_name(source.stem + "_reversed.fur")
            else:
                default_out = source.with_name(source.stem + "_revfur.fur")
            current = output_var.get().lower()
            if not current or (extracting and not current.endswith(".furseq.json")) or (not extracting and not current.endswith(".fur")):
                output_var.set(str(default_out))
        run_button.configure(text="Reverse Sequence" if reversing else "Analyze Playback" if analyzing else "Run conversion")

    operation_combo.bind("<<ComboboxSelected>>", update_operation)

    loop_options = ttk.Frame(root)
    ttk.Checkbutton(
        loop_options,
        text="Loop output back to the beginning (insert Furnace 0B00; infinite loop)",
        variable=loop_back_var,
    ).pack(anchor="w")

    info_box = ttk.LabelFrame(root, text="Safety", padding=10)
    info_box.pack(fill="x", pady=(0, 10))
    ttk.Label(info_box, text=("RevFUR only changes the order table and pattern blocks. "
        "Original instruments, samples, wavetables and chip configuration are kept byte-for-byte."),
        wraplength=700, foreground="#38556f").pack(anchor="w")

    controls = ttk.Frame(root)
    controls.pack(fill="x", pady=(0, 8))
    run_button = ttk.Button(controls, text="Run conversion")
    run_button.pack(side="right")
    ttk.Label(controls, textvariable=status_var, wraplength=520).pack(side="left", fill="x", expand=True)

    log_frame = ttk.LabelFrame(root, text="Console log", padding=8)
    log_frame.pack(fill="both", expand=True)
    log_text = tk.Text(log_frame, height=13, wrap="word", state="disabled",
                       bg="#101820", fg="#d7e2ed", insertbackground="white",
                       font=("Consolas", 9), relief="flat")
    scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    log_text.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    log_text.pack(side="left", fill="both", expand=True)

    def append_log(text):
        log_text.configure(state="normal")
        log_text.insert("end", text.rstrip() + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")

    def set_module(value):
        module_var.set(value)
        if value:
            if not operation.get().startswith("Analyze"):
                if operation.get().startswith("Apply"):
                    if not output_var.get():
                        output_var.set(str(Path(value).with_name(Path(value).stem + "_revfur.fur")))
                elif operation.get().startswith("Reverse"):
                    if not output_var.get():
                        output_var.set(str(Path(value).with_name(Path(value).stem + "_reversed.fur")))
                elif not output_var.get():
                    output_var.set(str(Path(value).with_suffix(".furseq.json")))
            status_var.set("Ready: " + Path(value).name)

    def on_drop(event):
        try:
            paths = root.tk.splitlist(event.data)
            for path in paths:
                path = path.strip().strip("{}")
                if path.lower().endswith(".fur"):
                    set_module(path)
                elif path.lower().endswith(".furseq.json") or path.lower().endswith(".json"):
                    sequence_var.set(path)
                    if module_var.get() and not output_var.get():
                        suffix = "_reversed.fur" if operation.get().startswith("Reverse") else "_revfur.fur"
                        output_var.set(str(Path(module_var.get()).with_name(Path(module_var.get()).stem + suffix)))
        except Exception as exc:
            messages.put(("log", "Drop error: %s" % exc))

    if dnd_enabled:
        drop.drop_target_register(DND_FILES)
        drop.dnd_bind("<<Drop>>", on_drop)
    else:
        ttk.Label(root, text="Drag-and-drop is unavailable; use Browse. Install tkinterdnd2 to enable it.",
                  foreground="#777777").pack(anchor="w", pady=(3, 0))

    def execute():
        src = Path(module_var.get().strip().strip('"')) if module_var.get().strip() else None
        if src is None or not src.is_file():
            messagebox.showerror("Missing module", "Choose or drop a valid Furnace .fur module.")
            return
        if src.suffix.lower() != ".fur":
            messagebox.showerror("Unsupported module", "The input module must have a .fur extension.")
            return
        out = Path(output_var.get().strip().strip('"')) if output_var.get().strip() else None
        selected_operation = operation.get()
        is_apply = selected_operation.startswith("Apply")
        is_extract = selected_operation.startswith("Extract")
        is_reverse = selected_operation.startswith("Reverse")
        is_analyze = selected_operation.startswith("Analyze")
        seq = None
        if is_apply:
            seq = Path(sequence_var.get().strip().strip('"')) if sequence_var.get().strip() else None
            if seq is None or not seq.is_file():
                messagebox.showerror("Missing sequence", "Select an edited .furseq.json file.")
                return
            if out is None:
                out = src.with_name(src.stem + "_revfur.fur")
        elif is_reverse:
            if out is None:
                out = src.with_name(src.stem + "_reversed.fur")
        elif is_analyze:
            out = None
        elif out is None:
            out = src.with_suffix(".furseq.json")
        run_button.configure(state="disabled")
        status_var.set("Working...")
        append_log("=" * 58)
        append_log("RevFUR started: %s" % operation.get())

        def worker():
            try:
                if is_apply:
                    report = apply_sequence(src, seq, out, lambda line: messages.put(("log", line)))
                    messages.put(("done", "Edited sequence applied successfully. Original assets remain unchanged."))
                elif is_reverse:
                    report = reverse_entire_sequence(
                        src, out, lambda line: messages.put(("log", line)),
                        loop_back_to_start=bool(loop_back_var.get()),
                    )
                    ending = "loops back to order 00 / row 00" if report["loop_back_to_start"] else "stops at FF00"
                    messages.put(("done", "The detected playback pass was reversed across all channels and %s. Output: %s" % (ending, out)))
                elif is_analyze:
                    report = analyze_fur_file(src, lambda line: messages.put(("log", line)))
                    messages.put(("done", "Playback analysis complete: %s. See the console log for loop and end positions." % report["termination"]))
                else:
                    generated, dump = export_sequence(src, out, lambda line: messages.put(("log", line)))
                    messages.put(("done", "Sequence exported: %s (text dump: %s)." % (generated, dump)))
            except Exception as exc:
                messages.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    run_button.configure(command=execute)

    def poll_messages():
        try:
            while True:
                kind, value = messages.get_nowait()
                if kind == "log":
                    append_log(value)
                elif kind == "done":
                    append_log("SUCCESS: " + value)
                    status_var.set("Completed successfully.")
                    run_button.configure(state="normal")
                    messagebox.showinfo("RevFUR", value)
                elif kind == "error":
                    append_log("ERROR: " + value)
                    status_var.set("Conversion failed.")
                    run_button.configure(state="normal")
                    messagebox.showerror("RevFUR - conversion failed", value)
        except queue.Empty:
            pass
        root.after(120, poll_messages)

    if initial_file:
        if str(initial_file).lower().endswith(".furseq.json"):
            sequence_var.set(str(initial_file))
            operation.set("Apply edited sequence to FUR")
            update_operation()
        else:
            set_module(str(initial_file))
    update_operation()
    poll_messages()
    root.mainloop()
    return 0


def main(argv=None):
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args:
        return launch_gui()
    if len(raw_args) == 1 and raw_args[0].lower().endswith((".fur", ".furseq.json")):
        return launch_gui(raw_args[0])
    parser = argparse.ArgumentParser(description="Disassemble and edit only the sequence data in Furnace .fur modules.")
    sub = parser.add_subparsers(dest="command")
    p_extract = sub.add_parser("extract", help="Export orders and patterns to an editable .furseq.json file")
    p_extract.add_argument("input", help="Source Furnace .fur module")
    p_extract.add_argument("-o", "--output", help="Output .furseq.json path")
    p_reverse = sub.add_parser("reverse", help="Reverse the entire song across all channels and write a new .fur module")
    p_reverse.add_argument("input", help="Source Furnace .fur module")
    p_reverse.add_argument("-o", "--output", help="Output reversed Furnace .fur path")
    p_reverse.add_argument("--loop-back", action="store_true",
                           help="Insert 0B00 at the end so playback loops back to order 00 / row 00")
    p_reverse.add_argument("--keep-boundary-empty-ticks", action="store_true",
                           help="Keep empty/navigation-only ticks at the start of the reversed song")
    p_reverse.add_argument("--compact-empty-ticks", action="store_true",
                           help="Also remove isolated one-tick empty rows (shortens the song; changes rhythm)")
    p_analyze = sub.add_parser("analyze", help="Detect playback loops, navigation commands and the song end")
    p_analyze.add_argument("input", help="Source Furnace .fur module")
    p_apply = sub.add_parser("apply", help="Apply an edited .furseq.json sequence to the original .fur module")
    p_apply.add_argument("input", help="The exact original Furnace .fur module")
    p_apply.add_argument("sequence", help="Edited .furseq.json file")
    p_apply.add_argument("-o", "--output", help="Output Furnace .fur path")
    args = parser.parse_args(raw_args)

    if args.command == "extract":
        try:
            paths = export_sequence(Path(args.input), Path(args.output) if args.output else None, print)
            print("Created:", paths[0])
            return 0
        except Exception as exc:
            print("RevFUR error:", exc, file=sys.stderr)
            return 1
    if args.command == "reverse":
        try:
            report = reverse_entire_sequence(
                Path(args.input), Path(args.output) if args.output else None,
                print, loop_back_to_start=args.loop_back,
                compact_empty_ticks=args.compact_empty_ticks,
                trim_boundary_empty_ticks=not args.keep_boundary_empty_ticks,
            )
            print(json_summary(report))
            return 0
        except Exception as exc:
            print("RevFUR error:", exc, file=sys.stderr)
            return 1
    if args.command == "analyze":
        try:
            report = analyze_fur_file(Path(args.input), print)
            print(json_summary(report) if "output" in report else json.dumps(report, indent=2))
            return 0
        except Exception as exc:
            print("RevFUR error:", exc, file=sys.stderr)
            return 1
    if args.command == "apply":
        try:
            report = apply_sequence(Path(args.input), Path(args.sequence), Path(args.output) if args.output else None, print)
            print(json_summary(report))
            return 0
        except Exception as exc:
            print("RevFUR error:", exc, file=sys.stderr)
            return 1

    return launch_gui()


def json_summary(report):
    if "output" not in report:
        return "Playback: {termination}; played rows: {played_rows}; loop: {loop_detected}.".format(**report)
    return "Wrote {output}; patterns replaced: {patterns_replaced}; asset blocks preserved.".format(**report)


if __name__ == "__main__":
    raise SystemExit(main())
