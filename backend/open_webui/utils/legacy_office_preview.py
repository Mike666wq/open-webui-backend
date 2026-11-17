import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def convert_legacy_ppt_to_pptx(source_path: str | Path, output_dir: str | Path, timeout: int = 60) -> Path:
    """Convert legacy .ppt to .pptx using the image's LibreOffice runtime."""
    source = Path(source_path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    command = [
        'soffice',
        '--headless',
        '--convert-to',
        'pptx',
        '--outdir',
        str(output),
        str(source),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(
            'Legacy PPT preview requires LibreOffice (soffice) in the backend image.'
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Legacy PPT preview conversion timed out.') from exc

    converted = next(output.glob('*.pptx'), None)
    if result.returncode != 0 or converted is None:
        detail = (result.stderr or result.stdout or '').strip()
        log.warning('LibreOffice legacy PPT conversion failed: %s', detail)
        raise RuntimeError('LibreOffice could not convert this legacy PPT for preview.')

    return converted
