# -*- coding: utf-8 -*-
"""
테스트용 가상 데이터 생성기 (실제 인물/점수 아님)
  python tools/make_sample_data.py sample
생성물
  sample/reports/*.json        : 버크만 개인 결과 (두 가지 JSON 형태 혼합)
  sample/reports/*.pdf         : 텍스트 PDF 1개 (reportlab 이 있을 때만)
  sample/out/org_members.csv   : 조직 정보 (웹앱 입력)
"""
import json
import os
import random
import sys

random.seed(7)

COMP_EN = ["Social Energy", "Physical Energy", "Emotional Energy", "Self-Consciousness", "Assertiveness",
           "Insistence", "Incentives", "Restlessness", "Thought", "Autonomy", "Challenge"]
COMP_KO = ["사회적 에너지", "신체적 에너지", "감정적 에너지", "자의식", "주장성",
           "고집성", "인센티브", "변화", "사고", "자율성", "도전"]
JOB_EN = ["Artistic", "Clerical", "Literary", "Mechanical", "Musical", "Numerical", "Outdoor",
          "Persuasive", "Scientific", "Social Service"]
LAST = "김이박최정강조윤장임한오서신권황안송류홍"
FIRST = ["민준", "서연", "도윤", "지우", "하준", "서윤", "은우", "지민", "시우", "수아", "예준", "하은",
         "주원", "지아", "유준", "채원", "건우", "다은", "현우", "소율", "우진", "윤서", "선우", "지호"]

TEAMS = [
    ("전자소재연구팀", ["A파트", "B파트"]),
    ("생산기술팀", ["공정파트", "설비파트"]),
    ("경영기획팀", ["기획파트"]),
]


def rnd_point(bias):
    cx, cy = bias
    return round(min(98, max(2, random.gauss(cx, 18))), 1), round(min(98, max(2, random.gauss(cy, 18))), 1)


def make_person(i, team_bias):
    usual = rnd_point(team_bias)
    return {
        "interest": rnd_point((random.choice([30, 70]), random.choice([30, 70]))),
        "usual": usual,
        "needs": rnd_point((100 - usual[0] * 0.6, 100 - usual[1] * 0.7)),
        "stress": None,
        "comp": [(random.randint(5, 95), random.randint(5, 95)) for _ in COMP_EN],
        "org": [random.randint(10, 95) for _ in range(4)],
        "job": [random.randint(1, 99) for _ in JOB_EN],
    }


def shape_a(emp_id, name, email, p, signature):
    """중첩 dict 형태 (영문 키)"""
    d = {
        "reportType": "Birkman Signature Report" if signature else "Birkman Basic Report",
        "participant": {"name": name, "employeeId": emp_id, "email": email},
        "map": {
            "interests": {"x": p["interest"][0], "y": p["interest"][1]},
            "usualBehavior": {"x": p["usual"][0], "y": p["usual"][1]},
            "needs": {"x": p["needs"][0], "y": p["needs"][1]},
            "stress": {"x": p["needs"][0], "y": p["needs"][1]},
        },
        "organizationalFocus": {
            "Operations/Technology": p["org"][0], "Sales/Marketing": p["org"][1],
            "Administration/Fiscal": p["org"][2], "Design/Strategy": p["org"][3],
        },
        "interests": [{"name": n, "score": s} for n, s in zip(JOB_EN, p["job"])],
    }
    if signature:
        d["components"] = [{"name": n, "usual": u, "needs": nd} for n, (u, nd) in zip(COMP_EN, p["comp"])]
    return d


def shape_b(emp_id, name, email, p, signature):
    """한글 키 형태, 리스트/값 혼합"""
    d = {
        "진단종류": "시그니처" if signature else "베이직",
        "성명": name,
        "사번": emp_id,
        "버크만맵": [
            {"구분": "흥미", "가로": p["interest"][0] / 100, "세로": p["interest"][1] / 100},
            {"구분": "평소행동", "가로": p["usual"][0] / 100, "세로": p["usual"][1] / 100},
            {"구분": "욕구", "가로": p["needs"][0] / 100, "세로": p["needs"][1] / 100},
            {"구분": "스트레스", "가로": p["needs"][0] / 100, "세로": p["needs"][1] / 100},
        ],
        "조직지향점": {"실행": p["org"][0], "소통": p["org"][1], "관리": p["org"][2], "전략": p["org"][3]},
    }
    if signature:
        d["구성요소"] = {n: {"평소": u, "욕구": nd} for n, (u, nd) in zip(COMP_KO, p["comp"])}
    return d


KO_COLOR = {"red": "빨강", "green": "초록", "yellow": "노랑", "blue": "파랑"}
KO_JOB = ["숫자", "관리", "과학", "문학", "기술", "음악", "예술", "야외", "설득", "사회복지"]


def quad(x, y):
    return ("red" if x < 50 else "green") if y >= 50 else ("yellow" if x < 50 else "blue")


def shape_c(emp_id, name, email, p, signature):
    """실제 사내 추출 JSON 과 같은 구조 (map_symbols 중앙 원점 좌표, org_orientation, map_texts)"""
    def sym(layer, xy):
        return {"type": layer, "x": round((xy[0] - 50) / 50 * 4, 3), "y": round((xy[1] - 50) / 50 * 4, 3),
                "quadrant": KO_COLOR[quad(*xy)], "confidence": 0.99}
    xy = {"interest": p["interest"], "usual": p["usual"], "needs": p["needs"], "stress": p["needs"]}
    texts = {}
    for layer, v in xy.items():
        c = KO_COLOR[quad(*v)]
        texts[layer] = {"raw": "설명 문장입니다. 사분면에서 %s에 위치합니다.\n표시된 위치에 따르면:\n"
                               "• 예시 항목 하나\n• 예시 항목 둘이 줄바꿈으로\n이어짐\n• 예시 항목 셋\n외향\n사람지향\n내향\n과제지향" % c,
                        "color": c}
    org = dict(zip(["빨강", "초록", "노랑", "파랑"], p["org"]))
    return {
        "employee_id": emp_id, "name": name, "file_name": "%s_%s.pdf" % (emp_id, name),
        "report_type": "시그니처" if signature else "베이직",
        "map_texts": texts,
        "interests": [{"name": n, "score": sc} for n, sc in zip(KO_JOB, p["job"])],
        "org_path": ["테스트센터", "테스트팀"],
        "map_symbols": {k: sym(k, v) for k, v in xy.items()},
        "org_orientation": {"scores": org, "top_color": max(org, key=org.get), "confidence": 0.78},
    }


def main(base):
    rep = os.path.join(base, "reports")
    out = os.path.join(base, "out")
    for d in (rep, out):
        if not os.path.isdir(d):
            os.makedirs(d)
    rows = ["emp_id,name,email,team,part,role"]
    biases = {"전자소재연구팀": (65, 30), "생산기술팀": (30, 65), "경영기획팀": (40, 40)}
    n = 0
    used_names = set()
    for team, parts in TEAMS:
        size = 9 if len(parts) > 1 else 6
        for k in range(size):
            n += 1
            emp_id = "2019%03d" % n
            while True:
                name = random.choice(LAST) + random.choice(FIRST)
                if name not in used_names:
                    used_names.add(name)
                    break
            email = "user%03d@dongwoo.example.com" % n
            if k == 0:
                part, role = "", "팀장"
            elif k == 1 or (k == 5 and len(parts) > 1):
                part, role = parts[0 if k == 1 else 1], "파트장"
            else:
                part, role = parts[(k % len(parts))], "팀원"
            rows.append("%s,%s,%s,%s,%s,%s" % (emp_id, name, email, team, part, role))
            if k == size - 1:
                continue  # 미진단자
            p = make_person(n, biases[team])
            signature = (n % 5 == 0) or role == "팀장"
            data = (shape_c if n % 3 == 0 else shape_a if n % 2 else shape_b)(emp_id, name, email, p, signature)
            if n % 2 == 0 and n % 3 != 0:
                data.pop("사번", None)  # 파일명에서 사번 읽기 테스트
            with open(os.path.join(rep, "%s_%s.json" % (emp_id, name)), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out, "org_members.csv"), "w", encoding="utf-8-sig") as f:
        f.write("\n".join(rows) + "\n")
    try:
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(os.path.join(rep, "2019999_pdftest_signature.pdf"))
        y = 800
        c.drawString(50, y, "Birkman Signature Report  -  pdf.test@dongwoo.example.com")
        for nme in COMP_EN:
            y -= 20
            c.drawString(50, y, "%s   Usual %d   Needs %d" % (nme, random.randint(5, 95), random.randint(5, 95)))
        y -= 30
        c.drawString(50, y, "Organizational Focus")
        for nme in ["Operations", "Sales", "Administration", "Strategy"]:
            y -= 20
            c.drawString(50, y, "%s  %d" % (nme, random.randint(10, 95)))
        c.save()
    except ImportError:
        pass
    print("생성 완료:", base)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample")
