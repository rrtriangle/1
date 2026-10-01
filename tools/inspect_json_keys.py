# -*- coding: utf-8 -*-
"""
버크만 JSON 파일의 '키 구조'만 뽑아주는 도구 (값은 모두 가림)
개발자에게 형식을 알려줄 때 개인정보 없이 구조만 공유하기 위한 용도입니다.

  python inspect_json_keys.py 파일.json            # 구조만
  python inspect_json_keys.py 파일.json --labels   # 리스트 안 이름표(예: 구성요소 이름)도 표시
  -> 각 줄: 경로  |  값 종류(number/text/bool/null)  |  숫자면 범위 힌트(0~1 / 0~100 등)

* 리스트 항목은 [#] 로 표시합니다. 리스트 안의 이름표(name/label 등)는 --labels 를 줄 때만 보여줍니다.
* 공유 전에 결과에 사람 이름·메일이 없는지 한 번 확인해 주세요.
"""
import json
import sys


def kind(v):
    if v is None:
        return "null", ""
    if isinstance(v, bool):
        return "bool", ""
    if isinstance(v, (int, float)):
        if 0 <= v <= 1:
            rng = "0~1"
        elif 0 <= v <= 100:
            rng = "0~100"
        else:
            rng = "기타"
        return "number", rng
    if isinstance(v, str):
        if v.strip().replace(".", "", 1).isdigit():
            return "text(숫자)", ""
        if "@" in v:
            return "text(메일)", ""
        return "text(%d자)" % len(v), ""
    return type(v).__name__, ""


SHOW_LABELS = "--labels" in sys.argv
LABEL_KEYS = ("name", "title", "label", "key", "type", "category", "component", "area", "symbol", "layer",
              "이름", "항목", "구분", "영역", "명칭")


def walk(o, path, out):
    if isinstance(o, dict):
        for k, v in o.items():
            walk(v, path + [str(k)], out)
    elif isinstance(o, list):
        for v in o[:50]:
            walk(v, path + ["[#]"], out)
    else:
        k, r = kind(o)
        if (SHOW_LABELS and isinstance(o, str) and "[#]" in path and path[-1].lower() in LABEL_KEYS
                and "@" not in o and len(o) <= 40):
            k, r = "label", '"%s"' % o
        out.setdefault(".".join(path), set()).add((k, r))


def main(fn):
    raw = open(fn, "rb").read()
    for enc in ("utf-8-sig", "cp949", "utf-16"):
        try:
            data = json.loads(raw.decode(enc))
            break
        except Exception:  # noqa
            continue
    else:
        print("JSON 을 읽을 수 없습니다.")
        return
    out = {}
    walk(data, [], out)
    for p in sorted(out):
        kinds = sorted(out[p])
        print("%-70s | %s" % (p, ", ".join(("%s %s" % kr).strip() for kr in kinds)))


if __name__ == "__main__":
    main([a for a in sys.argv[1:] if not a.startswith("--")][0])
