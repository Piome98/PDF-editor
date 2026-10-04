"""OCR 모델 파일을 프로젝트의 models/ 폴더에 준비한다 (빌드 전에 한 번, 인터넷 필요).

RapidOCR 패키지에 들어 있는 검출/방향 모델은 복사하고, 한국어 인식 모델은 RapidOCR이 내려받게 한 뒤 복사한다.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.ocr import MODEL_FILES, model_dir  # noqa: E402


def main() -> int:
    import rapidocr
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    dest = model_dir()
    dest.mkdir(exist_ok=True)
    pkg_models = Path(rapidocr.__file__).parent / "models"
    if not (pkg_models / MODEL_FILES["rec"]).exists():
        print("한국어 인식 모델 내려받는 중...")
        RapidOCR(params={"Global.log_level": "error", "Rec.lang_type": LangRec.KOREAN,
                         "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE})
    for name in MODEL_FILES.values():
        src, dst = pkg_models / name, dest / name
        if not src.exists():
            print(f"모델을 찾을 수 없습니다: {src}")
            return 1
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
        print(f"  {name}  {dst.stat().st_size / 1e6:.1f}MB")
    print(f"모델 준비 완료: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
