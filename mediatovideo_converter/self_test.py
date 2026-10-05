"""Exercise the shipped GUI and video pipeline using only generated media."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


def _self_test_run(command: list[str]) -> str:
    """Run a real video command with a deadline and include failure diagnostics."""
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f'{Path(command[0]).name} failed: {result.stderr[-4000:]}')
    return result.stdout


def _self_test_gui() -> dict[str, Any]:
    """Realize the actual main window and MKV dialog through their public APIs."""
    import tkinter as tk
    from .WEB_UI import MediaToVideoApp, MkvToMp4Dialog

    root = tk.Tk()
    try:
        MediaToVideoApp(root)
        root.update()
        labels: dict[str, tk.Widget] = {}
        pending = list(root.winfo_children())
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            try:
                labels[str(widget.cget('text'))] = widget
            except tk.TclError:
                continue
        expected = ('Source', 'Output', '1. Scan source', '2. Convert videos',
                    'Open diagnostic log')
        for label in expected:
            widget = labels.get(label)
            if widget is None or not widget.winfo_viewable() or widget.winfo_width() <= 1:
                raise RuntimeError(f'The main window did not realize its {label!r} control.')
        MkvToMp4Dialog(root)
        root.update()
        dialogs = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        if not dialogs or not dialogs[0].winfo_viewable():
            raise RuntimeError('The MKV to MP4 dialog did not open.')
        return {'main_controls': list(expected), 'mkv_dialog': True, 'tk': root.tk.call('package', 'require', 'Tk')}
    finally:
        root.destroy()


def _self_test_video() -> dict[str, Any]:
    """Encode a synthetic MKV, convert via the app API, and probe the output."""
    from .converter import converter_convert, converter_convert_mkv_to_mp4, converter_find_tools, converter_verify_tools
    from .models import ConversionOptions, GroupingMode, NamingMode, OutputLayout, VideoFormat
    from .scanner import scanner_scan

    ffmpeg, ffprobe = converter_find_tools()
    tools_report = converter_verify_tools(ffmpeg, ffprobe)
    with tempfile.TemporaryDirectory(prefix='mediatovideo-smoke-') as temporary:
        source = Path(temporary) / 'generated.mkv'
        output = Path(temporary) / 'converted.mp4'
        _self_test_run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x120:r=10', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '0.5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)])
        converter_convert_mkv_to_mp4(source, output)
        probe = json.loads(_self_test_run([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(output)]))
        codecs = {stream['codec_name'] for stream in probe['streams']}
        if not {'h264', 'aac'}.issubset(codecs) or output.stat().st_size == 0:
            raise RuntimeError('Generated MP4 is missing H.264 video or AAC audio.')
        clips = Path(temporary) / 'camera' / '2026' / '10' / '05'
        clips.mkdir(parents=True)
        for name in ('001.media', '002.media'):
            (clips / name).write_bytes(source.read_bytes())
        scan = scanner_scan(Path(temporary) / 'camera', GroupingMode.DAY)
        summary = converter_convert(scan.groups, ConversionOptions(output_root=Path(temporary) / 'joined', output_layout=OutputLayout.MIRROR_DATES, video_format=VideoFormat.MKV_COPY, naming_mode=NamingMode.MONTH_DAY))
        if summary.failed_groups or len(summary.completed) != 1:
            raise RuntimeError(f'Generated folder conversion failed: {summary.failed_groups}')
        joined = json.loads(_self_test_run([ffprobe, '-v', 'error', '-show_format', '-of', 'json', str(summary.completed[0])]))
        duration = float(joined['format']['duration'])
        if duration < 0.9:
            raise RuntimeError('Folder output did not contain both generated clips.')
        return {'ffmpeg': ffmpeg, 'ffprobe': ffprobe, 'codecs': sorted(codecs), 'output_bytes': output.stat().st_size, 'folder_duration': duration, 'tools': tools_report}


def self_test_main(report_path: Path | None = None) -> int:
    """Write a machine-readable result; callers impose an overall GUI deadline."""
    from . import __version__
    from .runtime import runtime_check
    from .diagnostics import diagnostics_start, diagnostics_info, diagnostics_log_path, diagnostics_exception

    report: dict[str, Any] = {'version': __version__, 'python': sys.version, 'frozen': bool(getattr(sys, 'frozen', False)), 'path': os.environ.get('PATH', ''), 'ok': False}
    diagnostics_start()
    session_marker = uuid.uuid4().hex
    diagnostics_info('Application startup', version=__version__, mode='self-test',
                     self_test_id=session_marker)
    try:
        runtime_report = runtime_check()
        diagnostics_info('Runtime compatibility check passed', **runtime_report)
        report['gui'] = _self_test_gui()
        report['video'] = _self_test_video()
        video_tools = report['video']['tools']['tools']
        diagnostics_info('Video tools verified',
                         ffmpeg_version=video_tools['ffmpeg']['version'],
                         ffprobe_version=video_tools['ffprobe']['version'])
        diagnostics_info('Generated video self-test passed', **report['video'])
        log_path = diagnostics_log_path()
        if log_path is None:
            raise RuntimeError('The diagnostic log could not be created during the self-test.')
        saved_log = log_path.read_text(encoding='utf-8')
        # Restrict checks to this test's marker so an earlier successful launch
        # cannot mask missing readiness or version records in the current run.
        marker_index = saved_log.find(session_marker)
        if marker_index < 0:
            raise RuntimeError('The diagnostic log is missing this self-test session.')
        saved_log = saved_log[saved_log.rfind('\n', 0, marker_index) + 1:]
        for marker in ('Application startup', 'Application window initialized',
                       __version__, str(runtime_report['python_version']),
                       str(report['gui']['tk'])):
            if marker not in saved_log:
                raise RuntimeError(f'The diagnostic log is missing {marker!r}.')
        report['diagnostics'] = {'log_path': str(log_path), 'startup_logged': True,
                                 'gui_ready_logged': True, 'runtime_versions_logged': True}
        report['ok'] = True
    except Exception as error:
        diagnostics_exception('Application self-test failed', error)
        report['error'] = f'{type(error).__name__}: {error}'
    result = json.dumps(report, indent=2)
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(result + '\n', encoding='utf-8')
    if sys.stdout:
        print(result)
    return 0 if report['ok'] else 1
