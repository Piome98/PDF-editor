"""템플릿과 영역 데이터 모델. GUI/PDF 라이브러리에 의존하지 않는다."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, fields

ERASE = "erase"   # 영역 안의 내용을 지움
VALUE = "value"   # 영역 안의 숫자를 새 값으로 교체

ERASE_ALL = "all"     # 지우기 영역: 영역 전체를 덮음 (글자·도장 이미지 등 모두)
ERASE_PICK = "pick"   # 지우기 영역: 영역 안의 글자 중 고른 단어만 지움

TEMPLATE_VERSION = 1


@dataclass
class Region:
    page: int                    # 0부터 시작하는 페이지 번호
    rect: list[float]            # [x0, y0, x1, y1], PDF 포인트 단위
    kind: str = VALUE
    name: str = ""
    formula: str = ""            # 비어 있으면 사용자가 문서마다 값을 직접 입력
    decimals: int = 0
    thousands: bool = True
    prefix: str = ""
    suffix: str = ""
    align: str = "right"         # left | center | right
    font_size: float = 0         # 0 = 원본 글자 크기 자동 감지
    color: str = ""              # "" = 원본 색 자동 감지, 아니면 "#RRGGBB"
    fill: str = ""               # "" = 배경 유지(글자만 제거), 아니면 "#RRGGBB"로 덮음
    sample_text: str = ""        # 템플릿을 만들 때 영역 안에 있던 텍스트 (참고용)
    # 숫자 영역: True면 숫자가 들어 있는 단어만 교체하고 라벨·단위 글자는 남긴다
    number_only: bool = True
    # 지우기 영역 (ERASE_PICK일 때): 단어 텍스트 기준 규칙이라 다른 문서에도 그대로 적용된다
    erase_mode: str = ERASE_ALL
    keep_texts: list[str] = field(default_factory=list)    # 이 글자는 남김
    erase_texts: list[str] = field(default_factory=list)   # 이 글자는 지움
    unknown_action: str = "erase"                           # 규칙에 없는 글자: "erase" | "keep"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def should_erase(self, word: str) -> bool:
        if self.erase_mode != ERASE_PICK:
            return True
        if word in self.erase_texts:
            return True
        if word in self.keep_texts:
            return False
        return self.unknown_action == "erase"

    def set_erase(self, word: str, erase: bool) -> None:
        for lst in (self.keep_texts, self.erase_texts):
            while word in lst:
                lst.remove(word)
        (self.erase_texts if erase else self.keep_texts).append(word)


@dataclass
class Template:
    name: str = "새 템플릿"
    page_sizes: list[list[float]] = field(default_factory=list)  # [[w, h], ...]
    regions: list[Region] = field(default_factory=list)
    font_file: str = ""          # 비어 있으면 맑은 고딕 등 시스템 한글 글꼴 사용

    def to_json(self) -> str:
        data = {"version": TEMPLATE_VERSION, **asdict(self)}
        return json.dumps(data, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Template":
        data = json.loads(text)
        region_keys = {f.name for f in fields(Region)}
        regions = [Region(**{k: v for k, v in r.items() if k in region_keys})
                   for r in data.get("regions", [])]
        return cls(
            name=data.get("name", "새 템플릿"),
            page_sizes=data.get("page_sizes", []),
            regions=regions,
            font_file=data.get("font_file", ""),
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "Template":
        with open(path, encoding="utf-8") as f:
            return cls.from_json(f.read())

    def region(self, region_id: str) -> Region | None:
        return next((r for r in self.regions if r.id == region_id), None)

    def next_name(self, base: str) -> str:
        used = {r.name for r in self.regions}
        i = 1
        while f"{base}{i}" in used:
            i += 1
        return f"{base}{i}"
