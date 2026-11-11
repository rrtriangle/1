class_name Game
extends RefCounted
## 시뮬레이션 규칙 (데모 v0.4, 8주). 화면과 분리되어 있어서 수치만 바꿔도 밸런스를 조정할 수 있다.

signal alert(text: String, kind: String)
signal log_line(text: String)

# ---------------------------------------------------------------- 시간
const HOURS_PER_SHIFT := 12
const WIN_DAY := 56            # 이 날의 야간을 버티면 데모 종료 (8주)
const WEEK_DAYS := 7
const PROTECT_DAYS := 7        # 이 날까지는 퇴사·실종이 없다 (「심부름」 제외)
const ERRAND_DAY := 2          # 「심부름」이 오는 밤

# ---------------------------------------------------------------- 경제 (만 원)
const SELL_PRICE := 50.0
const RAW_COST := 20.0
const WAGE_PER_SHIFT := 15.0   # 직원 1인당 교대당 급여 (쉬는 날에도 나간다)
const NIGHT_BONUS := 8.0       # 야간 근무자 가산 (1.5배)
const OVERTIME_BONUS := 8.0    # 연장 교대 가산 (1.5배)
const UPKEEP_PER_SHIFT := 40.0
const UPKEEP_PER_ROOM := 4.0   # 새로 지은 방 하나당 교대당 유지비
const START_MONEY := 800.0
const REPAIR_COST := 150.0
const REFINE_RATE := 0.9       # 정제실 직원 1명(운영 50)의 시간당 처리량
const SECOND_WORKER := 0.5     # 같은 방 두 번째 직원의 기여
const HAUL_PER_HOUR := 4.0     # 창고 직원 1명이 시간당 입고하는 드럼 수
const RAW_CAP := 12.0          # 반응실 하나당 미정제 탱크 용량
const OUTBOX_CAP := 3.0        # 정제실 하나당 출구에 쌓아 둘 수 있는 드럼 (창고 직원이 없으면 여기서 막힌다)
const STOCK_CAP := 60.0        # 물류 창고 하나당 재고 한도
const TRUCK_LOAD := 12.0       # 트럭 1회 최대 적재
const TRUCK_HOURS := [2, 8]    # 주간 10시, 16시
const EXTRA_TRUCK_HOUR := 5    # 물류 창고가 둘이면 13시 트럭 추가

# ---------------------------------------------------------------- 인사와 근로시간
const MIN_STAFF := 3
const START_TO := 6
const AD_COST := 100.0
const AD_DELAY_DAYS := 3
const LEGAL_SHIFTS := 4        # 주 기본 교대 (48시간)
const OVERTIME_SHIFTS := 1     # 합법 연장 교대 (가산수당)
const EXT_TRUST_COST := 5      # 특별연장 인가: 그 주 한도 +1, 신뢰도 소모
const START_TRUST := 50
const TRUST_PROFIT := 2
const TRUST_DEFICIT := 4

# ---------------------------------------------------------------- 공간과 전력
const POWER_START := 7
const POWER_PER_ZONE := 2      # 구역을 탈환할 때마다
const OUTAGE_LOSS := 2         # 정전 때 줄어드는 용량 (그 교대 동안)
const OUTAGE_P := 0.04
const CCTV_COST := 150.0
const EM_COST := 40.0
const REST_NOISE := 0.75       # 생산동 휴게실의 휴식 효과
const CORR_BASE := 0.03        # 밤에 혼자 복도를 걸을 때, 구역 하나당 노출 확률
const CORR_DARK := 0.08        # 지나는 복도가 전부 어두울 때 더해지는 확률

const ZONE_ORDER := ["research", "reactor2", "refinery", "office", "prod", "dock", "gate", "wh_a", "wh_b"]
const ZONE_DEF := {
	"research": {"name": "연구동", "rect": Rect2(4, 6, 212, 190), "open": false},
	"reactor2": {"name": "2번 반응기동", "rect": Rect2(226, 6, 268, 190), "open": false},
	"refinery": {"name": "정제동", "rect": Rect2(504, 6, 236, 190), "open": false},
	"office": {"name": "사무동", "rect": Rect2(4, 208, 212, 204), "open": true},
	"prod": {"name": "1구역 생산동", "rect": Rect2(226, 208, 268, 204), "open": true},
	"dock": {"name": "출하장", "rect": Rect2(504, 208, 236, 204), "open": true},
	"gate": {"name": "정문·경비실", "rect": Rect2(4, 424, 212, 198), "open": true},
	"wh_a": {"name": "물류 창고 A", "rect": Rect2(226, 424, 268, 198), "open": true},
	"wh_b": {"name": "물류 창고 B", "rect": Rect2(504, 424, 236, 198), "open": false},
}
const ZONE_LINKS := [["gate", "office"], ["office", "prod"], ["prod", "dock"], ["prod", "wh_a"], ["wh_a", "wh_b"],
	["prod", "refinery"], ["prod", "reactor2"], ["office", "research"]]
## 탈환 조건 (기획서 14장). 연구동은 연구 시스템과 함께 다음 단계에서.
const RECLAIM := {
	"wh_b": {"day": 8, "trust": 40, "cost": 800.0, "shifts": 3, "to": 1},
	"refinery": {"day": 1, "trust": 50, "cost": 1500.0, "shifts": 5, "to": 1},
	"reactor2": {"day": 29, "trust": 0, "cost": 2500.0, "shifts": 6, "to": 1},
}
const RECLAIM_ORDER := ["wh_b", "refinery", "reactor2"]

## 칸: [id, 구역, 크기, 처음 지어진 방]
const SLOT_DEF := [
	["office_M", "office", "M", ""], ["office_S1", "office", "S", "qc"], ["office_S2", "office", "S", ""],
	["prod_L", "prod", "L", "reactor"], ["prod_M1", "prod", "M", "refine"], ["prod_M2", "prod", "M", ""],
	["wha_L", "wh_a", "L", "warehouse"], ["wha_S", "wh_a", "S", ""],
	["whb_L1", "wh_b", "L", ""], ["whb_L2", "wh_b", "L", ""],
	["ref_M1", "refinery", "M", ""], ["ref_M2", "refinery", "M", ""], ["ref_M3", "refinery", "M", ""],
	["r2_L1", "reactor2", "L", ""], ["r2_L2", "reactor2", "L", ""],
]
const SLOT_W := {"L": 100.0, "M": 70.0, "S": 50.0}
const SLOT_H := 112.0
const SIZE_RANK := {"S": 1, "M": 2, "L": 3}
const SIZE_NAME := {"S": "소", "M": "중", "L": "대"}
## 방 종류. zones가 비어 있으면 크기만 맞으면 어디든.
const ROOM_TYPES := {
	"reactor": {"name": "반응실", "size": "L", "cost": 800.0, "zones": ["prod", "reactor2", "wh_b"]},
	"refine": {"name": "정제실", "size": "M", "cost": 400.0, "zones": ["prod", "refinery", "reactor2"]},
	"warehouse": {"name": "물류 창고", "size": "L", "cost": 300.0, "zones": ["wh_a", "wh_b", "reactor2"]},
	"rest": {"name": "휴게실", "size": "M", "cost": 150.0, "zones": []},
	"qc": {"name": "QC실", "size": "S", "cost": 200.0, "zones": ["office"]},
	"smoke": {"name": "흡연실", "size": "S", "cost": 60.0, "zones": []},
	"purify": {"name": "정화 작업", "size": "L", "cost": 0.0, "zones": []},
}
const BUILD_ORDER := ["rest", "refine", "reactor", "warehouse", "smoke"]
const WORK_TYPES := ["reactor", "refine", "warehouse", "qc", "purify"]
const ASSIGN_TYPES := ["reactor", "refine", "warehouse", "rest", "qc", "purify"]

# ---------------------------------------------------------------- 이상현상
## 역압: 시간당 확률 = PRESSURE_K × ((가동률-80)/20)² (야간 ×1.5)
const PRESSURE_K := 0.04
const PRESSURE_AUTO_OK := 0.6
const PRESSURE_DIRECT_BASE := 0.3
const PRESSURE_DIRECT_PER_OP := 0.0065
const INJ_AUTO := 0.15         # 사고 때 반응실 직원 1명당 부상 확률
const INJ_DIRECT := 0.3
const PERFECT_P := 0.2         # 직접 지시가 성공했을 때 "완벽한 차단"이 뜰 확률
## 오염 5단계 (25마다 한 단계, 100이면 변이)
const CONTAM_REST_SLOW := 0.7  # 다크서클 이상: 피로 회복 70%
const CONTAM_ROOM_BONUS := 0.04  # 검은 무늬 이상인 사람이 있는 방: '한 명 더' 확률 증가
const MUTANT_SPREAD := 1.5     # 변이한 사람과 같은 방: 밤에 시간당 오염
const ERRAND_DRIFT := 4.0      # 「심부름」 직원: 밤 근무마다 오염
const OFF_DECAY := 0.5         # 쉬는 교대마다 오염 감소
const LEAVE_DAYS := 3
const LEAVE_DECAY := 8.0       # 휴직 중 교대마다 오염 감소
const PURIFY_STAFF_COST := 300.0
const PURIFY_STAFF_AMOUNT := 40.0

## 한 명 더
const EXTRA_BASE := 0.04
const EXTRA_DARK := 0.05
const EXTRA_VANISH := 0.35     # 경고를 두 번 받은 직원이 또 방치됐을 때 시간당 실종 확률

# ---------------------------------------------------------------- 감사
const AUDIT_PASS_MAX := 2      # 1차·정기 감사: 지적이 이 수 이하면 통과
const SPECIAL_PASS_MAX := 1
const GRACE_WEEKS := 2
const REGULAR_WEEKS := [4, 8]  # 정기감사 주간. 2주 전 결산에서 예고한다
const RECORDS_FOR_INTEL := 20
const WATCH_CAP := 2
const LAST_WEEK_CAP := 3
const AUDITOR_TYPES := ["서류파", "현장파", "숫자파", "형식파"]
const AUDITOR_INFO := {
	"서류파": {"arrive": "서류철을 한 아름 들고 와서 \"지난 보고서랑 근무 기록 전부 주세요.\"", "watch": "보고서·근로시간 장부 대조. 숨긴 사고를 잘 찾는다", "reward": "예산 +200"},
	"현장파": {"arrive": "안전화 차림으로 오자마자 순찰을 돈다.", "watch": "밤에 혼자 일하는 방, 꺼진 조명. 공장에 머물러 인원수가 하나 늘어난다", "reward": "2주간 수리비 절반"},
	"숫자파": {"arrive": "노트북을 열고 첫 질문이 \"가동률 몇 퍼센트로 돌리세요?\"", "watch": "가동률 80% 초과 운영, 근로시간 숫자. 이상현상 은폐는 잘 못 찾는다", "reward": "2주간 유지비 30% 감면"},
	"형식파": {"arrive": "늦게 와서 커피부터 찾는다.", "watch": "대충 본다", "reward": "없음"},
}
## 숨긴 사고를 찾아낼 확률 (유형 × 사고 종류)
const DETECT := {
	"서류파": {"현장": 0.6, "인사": 0.9},
	"현장파": {"현장": 0.7, "인사": 0.4},
	"숫자파": {"현장": 0.3, "인사": 0.2},
	"형식파": {"현장": 0.15, "인사": 0.15},
}
## 근로시간 위반이 지적으로 잡히는 상한 (유형별)
const LEDGER_CAP := {"서류파": 3, "숫자파": 2, "현장파": 1, "형식파": 1}
const DOUBLE_STAMP_MIN := 3    # 서류파·숫자파 감사 주간에 「심부름」 직원이 이만큼 일하면 지적
const AUDITOR_NAMES := ["권 차장", "문 과장", "남궁 부장", "한 차장", "육 과장", "배 부장"]
const APPLICANT_NAMES := ["윤 사원", "서 사원", "고 기사", "장 사원", "임 기사", "하 사원", "노 사원", "구 기사", "도 사원", "봉 기사", "석 사원", "탁 기사"]
const APPLICANT_TRAITS := ["꼼꼼함", "호기심 많음", "안전 제일", "고집불통", "꾸준함", "줄담배", ""]

var rng := RandomNumberGenerator.new()
var day: int = 1
var shift: String = "야간"   # 현재(또는 다음) 교대. 프롤로그는 밤에 도착한다.
var hour: int = 0
var money: float = START_MONEY
var trust: int = START_TRUST
var records: int = 0
var reactor_rate: int = 100
var raw_buffer: float = 0.0
var outbox: float = 0.0
var stock: float = 0.0
var finished_shift: float = 0.0
var shipped_shift: float = 0.0
var anomaly_count_shift: int = 0
var vanished_shift: int = 0
var pending_pressure: Array[String] = []
var night_pay: float = 0.0
var overtime_pay: float = 0.0
var total_shipped: float = 0.0

# 공간
var zones: Dictionary = {}       # id -> {"open", "corr_light", "reclaiming", "purify_left"}
var slots: Dictionary = {}       # id -> {"id", "zone", "size", "rect", "room"}
var slot_order: Array[String] = []
var rooms: Dictionary = {}       # id -> 방
var built_rooms: int = 0
var power_cap: int = POWER_START
var outage: int = 0
var shed: Array = []             # 정전으로 꺼진 조명 [["room"|"corr", id]]
var to_cap: int = START_TO

# 근로시간 장부
var ext_week: bool = false       # 이번 주 특별연장 인가
var viol_week: int = 0
var viol_last_week: int = 0
var violations_open: int = 0     # 아직 감사를 받지 않은 위반
var violations_total: int = 0
var shift_violations: PackedStringArray = PackedStringArray()
var risk_week: bool = false      # 이번 주 위험성평가 실시
var risk_last_week: bool = false
var disgraced: bool = false      # 중대사고 공표: 다음 공고 지원자 감소
var double_stamps: int = 0       # 감사 주간에 「심부름」 직원이 일한 교대 수 (출근 기록이 두 번씩 찍힌다)

# 주간 결산
var week_start_money: float = START_MONEY
var capex_week: float = 0.0       # 이번 주 투자비 (건설·탈환·설치). 주간 손익에서 뺀다
var week_ended: bool = false
var week_history: Array = []

# 감사: 0 없음, 1 감사 주간(1차 또는 정기, 1주 상주), 2 특별감사 유예
var audit_stage: int = 0
var auditor: Dictionary = {}      # {"type", "name", "special", "regular"}
var regular_next: Dictionary = {} # 예고된 정기감사 {"week", "type", "name"}
var deficit_carry: int = 0        # 정기감사로 넘겨진 적자 주
var special_type: String = ""
var special_known: bool = false
var findings: int = 0
var finding_notes: PackedStringArray = PackedStringArray()
var grace_left: int = 0
var grace_fresh: bool = false
var grace_origin: String = ""     # "적자" (1차 감사 불통과), "정기", "중대사고". 흑자로 취소되는 것은 "적자"뿐
var grace_profit_streak: int = 0
var grace_safe: bool = true
var grace_incidents: int = 0
var grace_deficits: int = 0
var intel_doc: bool = false
var pre_findings: int = 0
var aces: int = 0
var relaxed: bool = false
var auditor_room: String = ""
var solo_dark_hours: int = 0
var watch_findings: int = 0
var hidden: Array = []            # 숨긴 사고 [{"type", "kind", "day", "serious"}]
var shift_incidents: Array = []
var week_reported: int = 0
var perk_repair_weeks: int = 0
var perk_upkeep_weeks: int = 0
var replaced: bool = false
var audits_held: int = 0
var regular_held: int = 0
var specials_held: int = 0
var audit_log: Array = []         # [유형, 종류, 지적 수, 통과, 내역]
var serious_count: int = 0
var perfect_count: int = 0
var mutations: int = 0

# 채용
var ad_arrive_day: int = -1
var applicants: Array[Staff] = []

# 「심부름」
var errand_done: bool = false
var errand_target: Staff = null

var staff: Array[Staff] = []
var selected: Staff = null
var _walking: Array[Staff] = []


func _init() -> void:
	rng.randomize()
	for id in ZONE_ORDER:
		zones[id] = {"open": ZONE_DEF[id]["open"], "corr_light": false, "reclaiming": false, "purify_left": 0}
	var cursor := {}
	for d in SLOT_DEF:
		var z: String = d[1]
		var zr: Rect2 = ZONE_DEF[z]["rect"]
		var x: float = cursor.get(z, zr.position.x + 5.0)
		var w: float = SLOT_W[d[2]]
		slots[d[0]] = {"id": d[0], "zone": z, "size": d[2], "rect": Rect2(x, zr.position.y + 26.0, w, SLOT_H), "room": ""}
		slot_order.append(d[0])
		cursor[z] = x + w + 5.0
		if d[3] != "":
			_make_room(d[0], d[3])
	# 처음 켜져 있는 조명: 반응실, 정제실, QC실, 생산동·사무동·정문 복도 (전력 7/7). 물류 창고는 꺼져 있다.
	for id in ["prod_L", "prod_M1", "office_S1"]:
		rooms[id]["light"] = true
	for z in ["prod", "office", "gate"]:
		zones[z]["corr_light"] = true

	staff.append(Staff.new("김 반장", 52, 85, 40, 60, 35, PackedStringArray(["고집불통", "줄담배"]), "30년 일했는데 그런 거 없어."))
	staff.append(Staff.new("이 주임", 34, 60, 65, 45, 55, PackedStringArray(["꼼꼼함"]), "요즘 잠을 잘 못 자요."))
	staff.append(Staff.new("정 사원", 26, 35, 55, 50, 70, PackedStringArray(["호기심 많음"]), "창고 안쪽엔 뭐가 있어요?"))
	staff.append(Staff.new("오 기사", 41, 55, 45, 70, 60, PackedStringArray(["안전 제일"]), "수칙대로만 하면 됩니다."))
	staff.append(Staff.new("최 대리", 31, 50, 50, 55, 55, PackedStringArray(["꾸준함"]), "낮 근무가 체질이에요."))
	for s in staff:
		s.pos = entrance()


func _make_room(slot_id: String, type: String) -> void:
	var sl: Dictionary = slots[slot_id]
	var n := rooms_of(type).size()
	var nm: String = ROOM_TYPES[type]["name"]
	if n >= 1:
		nm = "%d호 %s" % [n + 1, nm]
	sl["room"] = type
	rooms[slot_id] = _room_dict(slot_id, type, nm, sl["zone"], sl["rect"], sl["size"])


func _room_dict(id: String, type: String, nm: String, zone: String, rect: Rect2, size: String) -> Dictionary:
	return {"id": id, "type": type, "name": nm, "zone": zone, "rect": rect, "size": size,
		"light": false, "cctv": false, "em": false, "alert": false, "down": false, "override": false,
		"extra": false, "extra_hours": 0, "victim": null}


func entrance() -> Vector2:
	var r: Rect2 = ZONE_DEF["gate"]["rect"]
	return r.get_center() + Vector2(0, 30)


func is_night() -> bool:
	return shift == "야간"


func week() -> int:
	return (day - 1) / WEEK_DAYS + 1


func clock_text() -> String:
	var start := 20 if is_night() else 8
	return "%02d:00" % ((start + hour) % 24)


func protected() -> bool:
	return day <= PROTECT_DAYS


# ---------------------------------------------------------------- 공간

func rooms_of(type: String) -> Array[String]:
	var out: Array[String] = []
	for id in rooms:
		if rooms[id]["type"] == type:
			out.append(id)
	return out


## 직원을 배치할 수 있는 방 (칸 순서, 정화 작업은 맨 뒤)
func assignable_rooms() -> Array[String]:
	var out: Array[String] = []
	for id in slot_order:
		if rooms.has(id) and ASSIGN_TYPES.has(rooms[id]["type"]):
			out.append(id)
	for id in rooms:
		if rooms[id]["type"] == "purify":
			out.append(id)
	return out


func room_label(id: String) -> String:
	if id == "off" or not rooms.has(id):
		return "휴무"
	var r: Dictionary = rooms[id]
	if r["type"] == "purify":
		return r["name"]
	return "%s (%s)" % [r["name"], ZONE_DEF[r["zone"]]["name"]]


func zone_of(room_id: String) -> String:
	if room_id == "off" or not rooms.has(room_id):
		return "gate"
	return rooms[room_id]["zone"]


## 구역 a에서 b까지 지나는 구역들 (양 끝 포함)
func path_zones(a: String, b: String) -> Array:
	if a == b:
		return [a]
	var prev := {a: ""}
	var queue: Array = [a]
	while not queue.is_empty():
		var cur: String = queue.pop_front()
		if cur == b:
			break
		for l in ZONE_LINKS:
			var nxt := ""
			if l[0] == cur:
				nxt = l[1]
			elif l[1] == cur:
				nxt = l[0]
			if nxt != "" and not prev.has(nxt):
				prev[nxt] = cur
				queue.append(nxt)
	var out: Array = []
	var c: String = b
	while c != "":
		out.push_front(c)
		c = prev.get(c, "")
	return out


func hops(a: String, b: String) -> int:
	return path_zones(a, b).size() - 1


func build_options(slot_id: String) -> Array[String]:
	var out: Array[String] = []
	var sl: Dictionary = slots[slot_id]
	if sl["room"] != "" or not zones[sl["zone"]]["open"]:
		return out
	for t in BUILD_ORDER:
		var d: Dictionary = ROOM_TYPES[t]
		if SIZE_RANK[d["size"]] > SIZE_RANK[sl["size"]]:
			continue
		var zs: Array = d["zones"]
		if not zs.is_empty() and not zs.has(sl["zone"]):
			continue
		out.append(t)
	return out


func build(slot_id: String, type: String) -> bool:
	if not build_options(slot_id).has(type):
		return false
	var cost: float = ROOM_TYPES[type]["cost"]
	if money < cost:
		return false
	money -= cost
	capex_week += cost
	_make_room(slot_id, type)
	built_rooms += 1
	log_line.emit("%s에 %s을(를) 지었다. (−%d)" % [ZONE_DEF[slots[slot_id]["zone"]]["name"], rooms[slot_id]["name"], cost])
	return true


## 탈환할 수 없는 이유. 빈 문자열이면 가능.
func reclaim_block(z: String) -> String:
	if not RECLAIM.has(z):
		return "데모에서는 열 수 없다"
	if zones[z]["open"]:
		return "개방됨"
	if zones[z]["reclaiming"]:
		return "정화 작업 중 (남은 %d교대, 2명 이상 배치)" % zones[z]["purify_left"]
	var d: Dictionary = RECLAIM[z]
	if day < d["day"]:
		return "%d일차 이후" % d["day"]
	if trust < d["trust"]:
		return "본사 신뢰도 %d 이상 필요 (현재 %d)" % [d["trust"], trust]
	if money < d["cost"]:
		return "예산 부족"
	return ""


func start_reclaim(z: String) -> bool:
	if reclaim_block(z) != "":
		return false
	var d: Dictionary = RECLAIM[z]
	money -= d["cost"]
	capex_week += d["cost"]
	zones[z]["reclaiming"] = true
	zones[z]["purify_left"] = d["shifts"]
	var zr: Rect2 = ZONE_DEF[z]["rect"]
	var id := "purify_" + z
	rooms[id] = _room_dict(id, "purify", "%s 정화" % ZONE_DEF[z]["name"], z, Rect2(zr.position.x + 5.0, zr.position.y + 26.0, zr.size.x - 10.0, SLOT_H), "L")
	log_line.emit("본사 승인. %s 정화 작업을 시작한다. 2명 이상을 %d교대 배치해야 한다. (−%d)" % [ZONE_DEF[z]["name"], d["shifts"], d["cost"]])
	return true


func install(room_id: String, what: String) -> bool:
	if not rooms.has(room_id):
		return false
	var r: Dictionary = rooms[room_id]
	if what == "cctv":
		if r["cctv"] or money < CCTV_COST:
			return false
		if power_used() + 1 > power_cap:
			log_line.emit("전력 여유가 없다. 조명을 하나 끄고 다시 설치하자.")
			return false
		money -= CCTV_COST
		capex_week += CCTV_COST
		r["cctv"] = true
		log_line.emit("%s에 CCTV를 달았다. (−%d, 전력 1)" % [r["name"], CCTV_COST])
		return true
	if what == "em":
		if r["em"] or money < EM_COST:
			return false
		money -= EM_COST
		capex_week += EM_COST
		r["em"] = true
		log_line.emit("%s에 비상 조명을 달았다. (−%d)" % [r["name"], EM_COST])
		return true
	return false


# ---------------------------------------------------------------- 전력

func room_power(id: String) -> int:
	return 2 if rooms[id]["size"] == "L" else 1


func power_limit() -> int:
	return power_cap - outage


func power_used() -> int:
	var n := 0
	for id in rooms:
		var r: Dictionary = rooms[id]
		if r["light"] and not (outage > 0 and r["em"]):
			n += room_power(id)
		if r["cctv"]:
			n += 1
	for z in zones:
		if zones[z]["corr_light"]:
			n += 1
	return n


func set_room_light(id: String, on: bool) -> bool:
	var r: Dictionary = rooms[id]
	if r["light"] == on:
		return true
	if on and not (outage > 0 and r["em"]) and power_used() + room_power(id) > power_limit():
		return false
	r["light"] = on
	return true


func set_corr_light(z: String, on: bool) -> bool:
	if not zones[z]["open"]:
		return false
	if zones[z]["corr_light"] == on:
		return true
	if on and power_used() + 1 > power_limit():
		return false
	zones[z]["corr_light"] = on
	return true


func _shed_power() -> PackedStringArray:
	var off: PackedStringArray = []
	while power_used() > power_limit():
		var cand: Array = []
		for id in rooms:
			if rooms[id]["light"] and not rooms[id]["em"]:
				cand.append(["room", id])
		for z in zones:
			if zones[z]["corr_light"]:
				cand.append(["corr", z])
		if cand.is_empty():
			break
		var c: Array = cand[rng.randi() % cand.size()]
		shed.append(c)
		if c[0] == "room":
			rooms[c[1]]["light"] = false
			off.append(rooms[c[1]]["name"])
		else:
			zones[c[1]]["corr_light"] = false
			off.append("%s 복도" % ZONE_DEF[c[1]]["name"])
	return off


# ---------------------------------------------------------------- 직원 조회

## 방 안에 있는 직원 (복도를 걷는 중인 사람은 빼고)
func present_in(id: String) -> Array[Staff]:
	var out: Array[Staff] = []
	for s in staff:
		if s.on_map() and s.room == id and s.transit == 0:
			out.append(s)
	return out


func working_in(id: String) -> Array[Staff]:
	var out: Array[Staff] = []
	for s in present_in(id):
		if s.can_work():
			out.append(s)
	return out


func headcount(id: String) -> int:
	var n := present_in(id).size()
	if rooms[id]["extra"]:
		n += 1
	if auditor_room == id:
		n += 1
	return n


func alive_count() -> int:
	var n := 0
	for s in staff:
		if not s.gone():
			n += 1
	return n


# ---------------------------------------------------------------- 근로시간

func legal_limit() -> int:
	return LEGAL_SHIFTS + OVERTIME_SHIFTS + (1 if ext_week else 0)


## 이 사람을 지금 배치하면: "" 문제 없음, "연장" 가산수당, 그 밖에는 위반 사유
func work_check(s: Staff) -> String:
	if s.worked_prev:
		return "연속 교대"
	var n := s.shifts_week + 1
	if n > legal_limit():
		return "주 한도 초과"
	if n > LEGAL_SHIFTS:
		return "연장"
	return ""


func plan_warnings(assign: Dictionary) -> PackedStringArray:
	var out: PackedStringArray = []
	for s in assign.keys():
		if assign[s] == "off" or s.gone():
			continue
		var c := work_check(s)
		if c != "" and c != "연장":
			out.append("%s(%s)" % [s.name, c])
	return out


func can_extend() -> bool:
	return not ext_week and trust > EXT_TRUST_COST


func request_extension() -> void:
	if not can_extend():
		return
	ext_week = true
	trust -= EXT_TRUST_COST
	log_line.emit("특별연장근로 인가를 받았다. 이번 주 한도 %d교대. (본사 신뢰도 −%d)" % [legal_limit(), EXT_TRUST_COST])


func _violation(s: Staff, what: String) -> void:
	viol_week += 1
	violations_open += 1
	violations_total += 1
	shift_violations.append("%s: %s" % [s.name, what])


# ---------------------------------------------------------------- 감사 상태

func in_audit() -> bool:
	return audit_stage == 1


func audit_status_text() -> String:
	match audit_stage:
		1:
			return "%s 중 (%s %s) · 지적 %d" % ["정기감사" if auditor.get("regular", false) else "1차 감사", auditor["type"], auditor["name"], findings]
		2:
			var t := "특별감사 유예 %d주 남음 (%s) · 흑자 연속 %d주" % [grace_left, grace_origin, grace_profit_streak]
			if special_known:
				t += " · 특별감사관: %s" % special_type
			return t
	if not regular_next.is_empty():
		return "정기감사 예고: %d주차 (%s %s)" % [regular_next["week"], regular_next["type"], regular_next["name"]]
	return "감사 없음"


## 회의 화면의 위험도 예보. assign: 직원 -> 방 id
func risk_forecast(assign: Dictionary) -> String:
	if not is_night():
		return "낮음 (주간에는 징후만 나타난다)"
	var score := 0
	var detail: PackedStringArray = []
	for id in rooms:
		var r: Dictionary = rooms[id]
		if not WORK_TYPES.has(r["type"]) and r["type"] != "rest":
			continue
		var n := 0
		for s in assign.keys():
			if assign[s] == id:
				n += 1
		if n == 1:
			score += 1
			if not r["light"]:
				score += 1
				detail.append("%s: 혼자, 소등" % r["name"])
			else:
				detail.append("%s: 혼자" % r["name"])
	if reactor_rate > 80:
		score += 1
		detail.append("가동률 %d%%" % reactor_rate)
	var level := "낮음"
	if score >= 3:
		level = "높음"
	elif score >= 1:
		level = "보통"
	if (risk_week or risk_last_week) and not detail.is_empty():
		level += " (" + ", ".join(detail) + ")"
	return level


## 사람 수에 맞춰 채울 자리. 반응실 → 정제실 → 물류 창고가 한 줄이고, 세 자리를 다 못 채우면 막힌 곳부터 푼다.
func posts_for(n: int) -> Array[String]:
	var out: Array[String] = []
	var re := rooms_of("reactor")
	var rf := rooms_of("refine")
	var wh := rooms_of("warehouse")
	if re.is_empty() or rf.is_empty() or wh.is_empty():
		return out
	if n >= 3:
		var lists := [re, rf, wh]
		for k in 3:
			for l in lists:
				if k < l.size():
					out.append(l[k])
	elif n == 2:
		if raw_buffer >= RAW_CAP * 0.5 or outbox >= OUTBOX_CAP * 0.5:
			out = [rf[0], wh[0]]  # 쌓인 것을 정제해서 창고로
		else:
			out = [re[0], rf[0]]
	elif n == 1:
		if outbox >= OUTBOX_CAP * 0.5:
			out = [wh[0]]
		elif raw_buffer >= RAW_CAP * 0.5:
			out = [rf[0]]
		else:
			out = [re[0]]
	return out


## 회의 화면이 미리 골라 주는 배치: 근로시간을 어기지 않는 사람만, 덜 일한 사람부터.
func suggest_plan() -> Dictionary:
	var out := {}
	var pool: Array[Staff] = []
	for s in staff:
		if s.gone():
			continue
		out[s] = "off"
		if s.can_be_assigned() and s.state != "붕괴" and work_check(s) in ["", "연장"]:
			pool.append(s)
	pool.sort_custom(func(a: Staff, b: Staff) -> bool:
		if a.shifts_week != b.shifts_week:
			return a.shifts_week < b.shifts_week
		return a.fatigue < b.fatigue)
	var posts := posts_for(pool.size())
	var n := mini(pool.size(), posts.size())
	var crew: Array[Staff] = pool.slice(0, n)
	crew.sort_custom(func(a: Staff, b: Staff) -> bool: return a.operation > b.operation)
	for i in n:
		out[crew[i]] = posts[i]
	# 정화 작업: 낮에 남는 사람이 둘 이상이면 보낸다
	if not is_night() and pool.size() - n >= 2:
		for id in rooms_of("purify"):
			out[pool[n]] = id
			out[pool[n + 1]] = id
			break
	return out


# ---------------------------------------------------------------- 채용

func can_post_ad() -> bool:
	return ad_arrive_day < 0 and applicants.is_empty() and alive_count() < to_cap and money >= AD_COST


func post_ad() -> void:
	if not can_post_ad():
		return
	money -= AD_COST
	ad_arrive_day = day + AD_DELAY_DAYS
	log_line.emit("일반 공고를 냈다. %d일차에 지원자가 온다. (−%d)" % [ad_arrive_day, AD_COST])


## 회의 때 호출: 공고 도착 처리
func check_applicants() -> void:
	if ad_arrive_day < 0 or day < ad_arrive_day:
		return
	ad_arrive_day = -1
	applicants.clear()
	var used := {}
	for s in staff:
		used[s.name] = true
	var pool: Array = APPLICANT_NAMES.duplicate()
	pool.shuffle()
	var want := 2 if disgraced else 3
	disgraced = false
	for n in pool:
		if applicants.size() >= want:
			break
		if used.has(n):
			continue
		var t: String = APPLICANT_TRAITS[rng.randi() % APPLICANT_TRAITS.size()]
		var tr := PackedStringArray([t]) if t != "" else PackedStringArray()
		var a := Staff.new(n, rng.randi_range(22, 45), rng.randi_range(25, 65), rng.randi_range(25, 65),
			rng.randi_range(30, 65), rng.randi_range(35, 70), tr, "")
		applicants.append(a)
	log_line.emit("지원자 %d명이 도착했다." % applicants.size())


func hire(a: Staff) -> void:
	if not applicants.has(a) or alive_count() >= to_cap:
		return
	a.pos = entrance()
	staff.append(a)
	applicants.clear()
	log_line.emit("%s을(를) 채용했다." % a.name)


# ---------------------------------------------------------------- 「심부름」

func errand_pending() -> bool:
	return not errand_done and day == ERRAND_DAY and is_night()


func errand_candidates() -> Array[Staff]:
	var out: Array[Staff] = []
	for s in staff:
		if s.can_be_assigned():
			out.append(s)
	return out


## target이 null이면 거절
func do_errand(target: Staff) -> String:
	errand_done = true
	if target != null:
		errand_target = target
		target.away = 1
		target.operation = minf(100.0, target.operation + 15.0)
		target.add_contamination(30.0)
		if not target.has_trait("심부름"):
			target.traits.append("심부름")
		log_line.emit("%s이(가) 박 씨의 심부름을 갔다." % target.name)
		return "%s이(가) 박 씨를 따라 정문 밖으로 나갔다. \"금방 끝나요.\"" % target.name
	var cands := errand_candidates()
	var v: Staff = cands[rng.randi() % cands.size()]
	_vanish(v, true)
	return "박 씨: \"그럼 할 수 없죠.\" 그날 밤, %s이(가) 출근 명단에서 사라졌다." % v.name


# ---------------------------------------------------------------- 교대

func start_shift() -> void:
	hour = 0
	finished_shift = 0.0
	shipped_shift = 0.0
	anomaly_count_shift = 0
	vanished_shift = 0
	solo_dark_hours = 0
	night_pay = 0.0
	overtime_pay = 0.0
	outage = 0
	shed.clear()
	shift_incidents.clear()
	shift_violations.clear()
	pending_pressure.clear()
	_walking.clear()
	for id in rooms:
		rooms[id]["down"] = false
		rooms[id]["immune"] = false
		rooms[id]["alert"] = false
		rooms[id]["override"] = false
	var night := is_night()
	for s in staff:
		s.take_snapshot()
		s.exposed_tonight = false
		s.omen_start = s.omen
		s.transit = 0
		s.smoked = false
		s.worked_now = false
		if s.gone():
			continue
		if s.room != "off" and not rooms.has(s.room):
			s.room = "off"
		if s.room != "off" and s.state == "붕괴":
			s.room = _flee_room(s)  # 무너진 사람은 배치해도 휴게실에 틀어박힌다 (없으면 집에 간다)
		if s.room != "off":
			s.pos = entrance()
			s.worked_now = true
			if s.worked_prev:
				_violation(s, "쉬지 않고 연속 교대")
			s.shifts_week += 1
			if in_audit() and s.has_trait("심부름"):
				double_stamps += 1
			if s.shifts_week > legal_limit():
				_violation(s, "주 %d교대째 (한도 %d)" % [s.shifts_week, legal_limit()])
			elif s.shifts_week > LEGAL_SHIFTS:
				overtime_pay += OVERTIME_BONUS
			if night:
				night_pay += NIGHT_BONUS
				s.consecutive_nights += 1
			else:
				s.consecutive_nights = 0
		elif night:
			s.consecutive_nights = 0
	# 현장파는 밤에 사람이 있는 방 하나에 머문다
	auditor_room = ""
	if in_audit() and auditor["type"] == "현장파" and night:
		var staffed: Array = []
		for id in rooms:
			if WORK_TYPES.has(rooms[id]["type"]) and not present_in(id).is_empty():
				staffed.append(id)
		if not staffed.is_empty():
			auditor_room = staffed[rng.randi() % staffed.size()]
	log_line.emit("[b]%d일차 %s 근무 시작[/b]" % [day, shift])
	if not shift_violations.is_empty():
		log_line.emit("[color=#e98]근로시간 위반: %s[/color]" % ", ".join(shift_violations))


## 교대 중 직접 지시: 다른 구역이면 한 시간 동안 복도를 걷는다
func order_move(s: Staff, to: String) -> void:
	if s.room == to or not s.on_map() or not rooms.has(to):
		return
	var a := zone_of(s.room)
	var b := zone_of(to)
	s.room = to
	var path := path_zones(a, b)
	if path.size() > 1:
		s.transit = 1
		s.transit_hops = path.size() - 1
		s.transit_path = path


func tick_hour() -> void:
	hour += 1
	var night := is_night()
	_walking.clear()

	for s in staff:
		if not s.on_map():
			continue
		if s.transit > 0:
			_walking.append(s)
			s.add_fatigue(0.5)
			if night:
				_corridor_tick(s)
			continue
		var r: Dictionary = rooms[s.room]
		if r["type"] == "rest":
			var eff: float = REST_NOISE if r["zone"] == "prod" else 1.0
			s.add_stress(-2.0 * eff)
			s.add_fatigue(-6.0 * eff * (CONTAM_REST_SLOW if s.contam_stage() >= 1 else 1.0))
		else:
			var f: float = (40.0 if night else 30.0) / HOURS_PER_SHIFT
			if r["type"] == "purify":
				f *= 1.2
			s.add_fatigue(f * (1.5 - s.stamina / 100.0))
			if night and s.consecutive_nights >= 2:
				s.add_stress(5.0 / HOURS_PER_SHIFT)
			if s.fatigue >= 95.0:
				s.add_stress(3.0)
			elif s.fatigue >= 70.0:
				s.add_stress(1.0)
		for o in present_in(s.room):
			if o == s:
				continue
			if s.contamination >= 50.0:
				o.add_stress(1.0)
			if s.contam_stage() >= 4 and night:
				o.add_contamination(MUTANT_SPREAD)
				o.add_stress(1.0)
			if s.state == "각성":
				o.add_stress(-2.0)

	_production()
	_audit_watch()

	if night and rng.randf() < OUTAGE_P:
		_power_outage()

	_pressure_tick(night)
	if night:
		_one_more_tick()
	if hour == 6:
		_smoke_break()

	for s in staff:
		_check_limits(s)
	for s in _walking:
		if s.transit > 0:
			s.transit -= 1


func _corridor_tick(s: Staff) -> void:
	for o in staff:
		if o != s and o.on_map() and o.transit > 0 and zone_of(o.room) == zone_of(s.room):
			return  # 같이 걷는 사람이 있다
	var path: Array = s.transit_path
	if path.is_empty():
		return
	var dark := 0
	for z in path:
		if not zones[z]["corr_light"]:
			dark += 1
	var p: float = s.transit_hops * (CORR_BASE + CORR_DARK * dark / path.size())
	if rng.randf() >= p:
		return
	anomaly_count_shift += 1
	s.exposed_tonight = true
	if rng.randf() < s.observation * 0.008:
		s.add_stress(15.0)
		records += 2
		s.notes.append("복도에서 누군가 나란히 걷고 있었다. 돌아보니 없었다")
	else:
		s.add_contamination(10.0)
		s.add_stress(10.0)
		s.notes.append("복도를 누구랑 같이 걸어왔다고 한다. 혼자였는데")


# ---------------------------------------------------------------- 생산

func _production() -> void:
	var reactors := rooms_of("reactor")
	var refines := rooms_of("refine")
	var whs := rooms_of("warehouse")
	var raw_cap: float = RAW_CAP * reactors.size()
	for id in reactors:
		if rooms[id]["down"]:
			continue
		var rs := working_in(id)
		rs.sort_custom(func(a: Staff, b: Staff) -> bool: return a.operation > b.operation)
		var eff := 0.0
		for i in mini(rs.size(), 2):
			var p: float = rs[i].operation / 50.0
			if rs[i].fatigue >= 70.0:
				p *= 0.8
			eff += p * (1.0 if i == 0 else SECOND_WORKER)
		raw_buffer = minf(raw_cap, raw_buffer + eff * 0.5 * reactor_rate / 100.0)
	var out_cap: float = OUTBOX_CAP * refines.size()
	for id in refines:
		var fs := working_in(id)
		fs.sort_custom(func(a: Staff, b: Staff) -> bool: return a.operation > b.operation)
		var cap := 0.0
		for i in mini(fs.size(), 2):
			var c: float = REFINE_RATE * fs[i].operation / 50.0
			if fs[i].has_trait("꼼꼼함"):
				c *= 0.85
			cap += c * (1.0 if i == 0 else SECOND_WORKER)
		var done: float = maxf(0.0, minf(minf(raw_buffer, cap), out_cap - outbox))
		raw_buffer -= done
		outbox += done
		finished_shift += done
	var stock_cap: float = STOCK_CAP * whs.size()
	for id in whs:
		var haul := 0.0
		for s in working_in(id):
			haul += HAUL_PER_HOUR * (0.7 if s.fatigue >= 70.0 else 1.0)
		var moved: float = maxf(0.0, minf(minf(haul, outbox), stock_cap - stock))
		outbox -= moved
		stock += moved
	# 트럭은 낮에만 온다
	if not is_night() and (TRUCK_HOURS.has(hour) or (hour == EXTRA_TRUCK_HOUR and (whs.size() >= 2 or reactors.size() >= 2))):
		var load: float = minf(TRUCK_LOAD, stock)
		if load > 0.0:
			stock -= load
			shipped_shift += load
			total_shipped += load
			log_line.emit("[color=gray]%s 트럭이 세정액 %.1f드럼을 싣고 나갔다.[/color]" % [clock_text(), load])


## 감사 중 감시: 현장파는 혼자·어두운 방, 유예 중에는 안전 운전 기록
func _audit_watch() -> void:
	if audit_stage == 2 and reactor_rate > 80:
		for id in rooms_of("reactor"):
			if not working_in(id).is_empty():
				grace_safe = false
	if not in_audit():
		return
	if auditor["type"] == "현장파" and is_night():
		for id in rooms:
			var r: Dictionary = rooms[id]
			if not WORK_TYPES.has(r["type"]):
				continue
			var n := present_in(id).size()
			if n == 1 or (n >= 1 and not r["light"]):
				solo_dark_hours += 1
				break


func _power_outage() -> void:
	if outage > 0:
		return
	outage = OUTAGE_LOSS
	var off := _shed_power()
	if off.is_empty():
		alert.emit("정전. 이번 교대 동안 전력 용량이 %d 줄어듭니다." % OUTAGE_LOSS, "info")
	else:
		alert.emit("정전. 전력 용량이 줄어 %s 조명이 꺼졌습니다." % ", ".join(off), "info")


## 줄담배: 교대 중간에 한 번 흡연실에 다녀온다 (한 시간 자리 비움)
func _smoke_break() -> void:
	var rooms_s := rooms_of("smoke")
	if rooms_s.is_empty():
		return
	for s in staff:
		if not s.on_map() or not s.can_work() or s.transit > 0 or s.smoked or not s.has_trait("줄담배"):
			continue
		var best := 99
		var path: Array = []
		for id in rooms_s:
			var p := path_zones(zone_of(s.room), rooms[id]["zone"])
			if p.size() - 1 < best:
				best = p.size() - 1
				path = p
		if best > 1:
			continue
		s.smoked = true
		s.add_stress(-10.0)
		s.transit = 1
		s.transit_hops = best
		s.transit_path = path if best > 0 else []
		s.notes.append("흡연실에 다녀왔다")


# ---------------------------------------------------------------- 역압

func _pressure_tick(night: bool) -> void:
	if reactor_rate <= 80:
		return
	for id in rooms_of("reactor"):
		var r: Dictionary = rooms[id]
		if r["down"] or r.get("immune", false) or pending_pressure.has(id):
			continue
		var rs := working_in(id)
		if rs.is_empty():
			continue
		for s in rs:
			s.add_stress(8.0 / HOURS_PER_SHIFT)
		var x: float = (reactor_rate - 80) / 20.0
		var p: float = PRESSURE_K * x * x
		if night:
			p *= 1.5
		if rng.randf() < p:
			pending_pressure.append(id)
			r["alert"] = true
			anomaly_count_shift += 1
			for s in rs:
				s.add_stress(5.0)
			alert.emit("%s 압력계 수치가 치솟고 있습니다." % r["name"], "pressure")


## 지금 결정해야 하는 역압에서, 작업중지를 요구하는 직원 (없으면 null)
func stopwork_staff() -> Staff:
	if pending_pressure.is_empty():
		return null
	for s in working_in(pending_pressure[0]):
		if s.has_trait("안전 제일"):
			return s
	return null


## 지금 결정해야 하는 역압의 성공 확률 (화면에 미리 보여준다)
func pressure_odds(direct: bool) -> float:
	if not direct or pending_pressure.is_empty():
		return PRESSURE_AUTO_OK
	var rs := working_in(pending_pressure[0])
	var avg := 0.0
	var bonus := 0.0
	for s in rs:
		avg += s.operation
		if s.has_trait("안전 제일"):
			bonus = 0.15
	if rs.size() > 0:
		avg /= rs.size()
	return clampf(PRESSURE_DIRECT_BASE + avg * PRESSURE_DIRECT_PER_OP + bonus, 0.0, 0.97)


## choice: 0 계기대로 감압, 1 직접 지시(반대쪽 밸브), 2 작업중지 요구 수용
func resolve_pressure(choice: int) -> void:
	if pending_pressure.is_empty():
		return
	var stopper := stopwork_staff()
	var odds_auto := pressure_odds(false)
	var odds_direct := pressure_odds(true)
	var id: String = pending_pressure.pop_front()
	var r: Dictionary = rooms[id]
	r["alert"] = false
	var rs := working_in(id)
	if choice == 2 and stopper != null:
		r["down"] = true
		for s in rs:
			s.add_stress(-5.0)
		stopper.notes.append("작업중지권 행사: 반응실을 세웠다")
		log_line.emit("[color=green]%s의 요구대로 작업을 멈췄다. 이번 교대 동안 %s은 가동하지 않는다.[/color]" % [stopper.name, r["name"]])
		return
	var direct := choice == 1
	var overridden := stopper != null
	if overridden:
		stopper.add_stress(8.0)
		stopper.notes.append("작업중지 요구가 받아들여지지 않았다")
	var success := rng.randf() < (odds_direct if direct else odds_auto)

	for s in rs:
		if rng.randf() < s.observation / 100.0:
			records += 1

	if success:
		var perfect := direct and rng.randf() < PERFECT_P
		for s in rs:
			if perfect:
				s.add_stress(-25.0)
				s.notes.append("★ 완벽한 차단: 밸브를 한 번에 잡았다")
			elif direct:
				s.add_stress(-15.0)
				s.notes.append("역압 위기 차단")
			else:
				s.notes.append("역압, 운 좋게 넘어감")
		if perfect:
			r["immune"] = true
			records += 2
			perfect_count += 1
			var who: String = rs[0].name if rs.size() > 0 else "직원"
			alert.emit("★ 완벽한 차단! %s이(가) 밸브를 한 번에 잡았다. 이번 교대 동안 %s에는 역압이 오지 않는다. (관찰 기록 +2)" % [who, r["name"]], "perfect")
		else:
			log_line.emit("[color=green]압력이 안정됐다.[/color]")
		return

	r["down"] = true
	var cost: float = REPAIR_COST * (0.5 if perk_repair_weeks > 0 else 1.0)
	money -= cost
	var injured := 0
	for s in rs:
		s.add_stress(15.0)
		s.notes.append("역압 사고 목격")
		if rng.randf() < (INJ_DIRECT if direct else INJ_AUTO):
			injured += 1
			s.state = "부상"
			s.down_days = 2
			s.transit = 0
			s.room = _flee_room(s)
			s.notes.append("역압 사고로 부상")
	_incident("설비 손상", "현장", false, overridden)
	if injured == 1:
		_incident("부상", "현장", false, overridden)
	elif injured >= 2:
		_incident("부상 %d명" % injured, "현장", true, overridden)
	# 같은 구역에 휴게실이 있으면 사고가 거기까지 번진다
	for rid in rooms_of("rest"):
		if rooms[rid]["zone"] == r["zone"]:
			for s in present_in(rid):
				s.add_stress(10.0)
				s.notes.append("쉬는 중에 옆 반응실 사고 소리를 들었다")
	alert.emit("%s 설비가 손상됐습니다. 이번 교대 동안 가동하지 않습니다. (수리비 %d)%s" % [r["name"], cost,
		" 부상 %d명." % injured if injured > 0 else ""], "info")


func _incident(t: String, kind: String, serious: bool = false, overridden: bool = false) -> void:
	shift_incidents.append({"type": t, "kind": kind, "day": day, "serious": serious, "override": overridden})
	if audit_stage == 2:
		grace_safe = false


# ---------------------------------------------------------------- 한 명 더

func _one_more_tick() -> void:
	var contaminated := false
	for s in staff:
		if s.on_map() and s.contamination >= 50.0:
			contaminated = true

	for id in rooms.keys():
		var r: Dictionary = rooms[id]
		if r["type"] == "smoke":
			continue
		var here := present_in(id)
		if r["extra"]:
			var victim: Staff = r["victim"]
			if not here.has(victim):
				_clear_extra(id)
				continue
			if _roll_detect(id, here):
				continue
			r["extra_hours"] += 1
			victim.exposed_tonight = true
			if r["extra_hours"] == 2:
				victim.add_contamination(20.0)
				victim.add_stress(15.0)
				victim.omen = mini(victim.omen + 1, 2)
				victim.clean_nights = 0
				victim.notes.append("명단에 없는 동료와 오래 함께 있었다")
				if victim.omen == 1:
					victim.notes.append("⚠ 이상 행동 시작: 혼잣말이 늘었다")
				else:
					victim.notes.append("⚠ 이상 행동 심화: 출근 기록이 두 번 찍힌다. 다시 혼자 두면 위험")
			elif r["extra_hours"] > 2 and victim.omen_start >= 2 and rng.randf() < EXTRA_VANISH:
				_vanish(victim)
				_clear_extra(id)
		else:
			var purify: bool = r["type"] == "purify"
			var mutant := false
			var marked := false
			var normal: Array[Staff] = []
			for o in here:
				if o.contam_stage() >= 4:
					mutant = true
				else:
					normal.append(o)
				if o.contam_stage() >= 3:
					marked = true
			# 변이한 사람이 있으면 둘이어도 '한 명 더'가 끼어든다
			if here.is_empty() or (here.size() != 1 and not purify and not mutant):
				continue
			if normal.is_empty():
				continue
			var v: Staff = normal[rng.randi() % normal.size()]
			var p := EXTRA_BASE
			if marked:
				p += CONTAM_ROOM_BONUS
			if not r["light"]:
				p += EXTRA_DARK
			if contaminated:
				p += 0.02
			if v.has_trait("호기심 많음"):
				p += 0.02
			if purify:
				p *= 1.5  # 폐쇄 구역은 넓고 어둡다
			if rng.randf() < p:
				r["extra"] = true
				r["extra_hours"] = 0
				r["victim"] = v
				anomaly_count_shift += 1
				if _roll_detect(id, here):
					continue
				if r["cctv"]:
					r["alert"] = true
					alert.emit("CCTV: %s 인원이 명단과 맞지 않습니다." % r["name"], "cctv")


func _roll_detect(id: String, here: Array[Staff]) -> bool:
	for s in here:
		var p: float = s.observation * 0.008
		if here.size() >= 2:
			for o in here:
				if o != s:
					p += o.observation * 0.003
		if s.has_trait("꼼꼼함"):
			p *= 1.25
		if s.state == "붕괴":
			p *= 0.3
		if s.fatigue >= 70.0:
			p *= 0.7
		if rng.randf() < p:
			_clear_extra(id)
			s.add_stress(20.0)
			s.notes.append("명단에 없는 사람을 발견")
			records += 4 if s.has_trait("호기심 많음") else 2
			for o in staff:
				if o != s and o.on_map():
					o.add_stress(5.0)
			alert.emit("%s: \"%s에 모르는 사람이 있었어요. 방금까지 분명히…\"" % [s.name, rooms[id]["name"]], "info")
			return true
	return false


func _clear_extra(id: String) -> void:
	var r: Dictionary = rooms[id]
	r["extra"] = false
	r["extra_hours"] = 0
	r["victim"] = null
	if r["cctv"]:
		r["alert"] = false


## forced: 「심부름」 거절처럼 보호 기간에도 일어나는 연출 실종
func _vanish(v: Staff, forced: bool = false) -> void:
	if protected() and not forced:
		v.add_contamination(15.0)
		v.add_stress(20.0)
		v.notes.append("어둠 속에서 누군가 손을 잡아끌었다. 겨우 돌아왔다")
		alert.emit("%s이(가) 잠시 위치가 확인되지 않다가 돌아왔습니다." % v.name, "info")
		return
	v.state = "실종"
	v.room = "off"
	v.transit = 0
	v.notes.append("교대 중 실종")
	vanished_shift += 1
	if not forced:
		_incident("실종", "인사", true)
	for o in staff:
		if o.gone():
			continue
		o.add_stress(30.0 if o.on_map() else 10.0)
	alert.emit("%s의 위치가 확인되지 않습니다." % v.name, "vanish")


# ---------------------------------------------------------------- 한계 판정

## 쉬러 갈 곳: 가장 가까운 휴게실. 없으면 집("off")
func _flee_room(s: Staff) -> String:
	var best := "off"
	var best_h := 99
	for id in rooms_of("rest"):
		var h := hops(zone_of(s.room), rooms[id]["zone"])
		if h < best_h:
			best_h = h
			best = id
	return best


func _send_away(s: Staff) -> void:
	var to := _flee_room(s)
	if to == "off":
		s.room = "off"
		s.transit = 0
	elif s.on_map():
		order_move(s, to)
	else:
		s.room = "off"


func _check_limits(s: Staff) -> void:
	if s.gone():
		return
	if s.contam_stage() >= 4 and not s.has_trait("변이"):
		s.traits.append("변이")
		s.notes.append("⚠ 변이: 오염이 끝까지 찼다. 같은 방 동료가 위험하다")
		mutations += 1
		alert.emit("%s의 눈이 검게 젖어 있다. 일은 여전히 잘한다. 같은 방에 있는 사람들이 이유 없이 불안해한다." % s.name, "mutate")
	if s.stress >= 150.0 and s.state != "실신":
		var roll := rng.randf()
		if protected() or roll < 0.5:
			s.state = "실신"
			s.down_days = 2
			s.stress = 90.0
			if s.on_map():
				s.transit = 0
				s.room = _flee_room(s)
			s.notes.append("완전 붕괴: 실신")
			_incident("실신", "인사")
			alert.emit("%s이(가) 쓰러졌습니다." % s.name, "break")
		elif roll < 0.8:
			s.state = "퇴사"
			s.room = "off"
			s.notes.append("사직서 제출")
			alert.emit("%s이(가) 사직서를 두고 공장을 떠났습니다." % s.name, "break")
		else:
			_vanish(s)
		return
	if s.stress >= 100.0 and s.state == "정상":
		var p: float = 0.15 + 0.002 * (s.mind - 50.0)
		if s.contamination >= 50.0:
			p += 0.10
		p = clampf(p, 0.05, 0.5)
		if rng.randf() < p:
			s.state = "각성"
			s.notes.append("각성: 침착")
			alert.emit("%s의 눈빛이 달라졌다. \"…괜찮아요. 제가 할게요.\"" % s.name, "awaken")
		else:
			s.state = "붕괴"
			s.notes.append("붕괴: 공황")
			var had_rest := not rooms_of("rest").is_empty()
			if s.on_map():
				_send_away(s)
			alert.emit("%s이(가) 무너졌다. 작업을 버리고 %s." % [s.name, "휴게실로 달아난다" if had_rest else "공장을 나가 버렸다"], "break")


# ---------------------------------------------------------------- 교대 종료

## 이번 주 영업 손익 (투자비 제외)
func week_profit() -> float:
	return money - week_start_money + capex_week


func upkeep() -> float:
	return UPKEEP_PER_SHIFT * (0.7 if perk_upkeep_weeks > 0 else 1.0) + UPKEEP_PER_ROOM * built_rooms


func end_shift() -> String:
	var night := is_night()
	var lines: PackedStringArray = []
	lines.append("[font_size=20][b]%d일차 %s조 근무 보고[/b][/font_size]" % [day, shift])
	if in_audit():
		lines.append("[color=#e98]감사 중: %s %s[/color]" % [auditor["type"], auditor["name"]])
	lines.append("")

	for s in staff:
		if s.gone():
			continue
		if s.room == "off":
			s.add_stress(-20.0)
			s.add_fatigue(-50.0 * (CONTAM_REST_SLOW if s.contam_stage() >= 1 else 1.0))
			s.add_contamination(-(LEAVE_DECAY if s.leave_days > 0 else OFF_DECAY))
		elif night and s.worked_now and s.has_trait("심부름"):
			s.add_contamination(ERRAND_DRIFT)
		elif night and anomaly_count_shift == 0:
			s.add_stress(-5.0)

	var any_staff := false
	for s in staff:
		var worked := s.worked_now or s.notes.size() > 0
		if s.gone():
			if s.notes.size() > 0:
				lines.append("[color=#d55]%s: %s[/color]" % [s.name, s.state])
				for n in s.notes:
					lines.append("    · " + n)
			continue
		if not worked:
			if s.away > 0:
				lines.append("[color=gray]%s: 심부름[/color]" % s.name)
			continue
		any_staff = true
		var line := "%s: 스트레스 %s, 피로 %s, 오염 %s" % [
			s.name, _delta(s.snap_stress, s.stress), _delta(s.snap_fatigue, s.fatigue), _delta(s.snap_contam, s.contamination)]
		if s.state != "정상":
			line += " [%s]" % s.state
		lines.append(line)
		for n in s.notes:
			if n.begins_with("⚠"):
				lines.append("    · [color=#fd6]%s[/color]" % n)
			else:
				lines.append("    · " + n)
	if not any_staff:
		lines.append("[color=gray]출근한 사람이 없다. 공장은 조용했다.[/color]")

	# 정화 작업
	for id in rooms.keys():
		if rooms[id]["type"] != "purify":
			continue
		var z: String = rooms[id]["zone"]
		var crew := working_in(id).size()
		if crew >= 2:
			zones[z]["purify_left"] -= 1
			if zones[z]["purify_left"] <= 0:
				lines.append(_open_zone(z))
			else:
				lines.append("%s 정화 작업: 남은 %d교대" % [ZONE_DEF[z]["name"], zones[z]["purify_left"]])
		elif crew == 1:
			lines.append("[color=gray]%s 정화 작업: 2명 이상이 있어야 진행된다[/color]" % ZONE_DEF[z]["name"])
	# 위험성평가
	for id in rooms_of("qc"):
		if not working_in(id).is_empty() and not risk_week:
			risk_week = true
			lines.append("위험성평가 실시: 이번 주 안전 의무 이행 기록에 남는다")

	var revenue := shipped_shift * (SELL_PRICE - RAW_COST)
	var wages := alive_count() * WAGE_PER_SHIFT + night_pay + overtime_pay
	money += revenue - wages - upkeep()
	lines.append("")
	lines.append("생산 %.1f드럼 · 출하 %.1f드럼 (+%d) · 정제실 출구 %.1f · 창고 재고 %.1f" % [finished_shift, shipped_shift, roundi(revenue), outbox, stock])
	var pay_note := ""
	if night_pay + overtime_pay > 0.0:
		pay_note = " (야간·연장 가산 %d 포함)" % roundi(night_pay + overtime_pay)
	lines.append("급여 −%d%s · 유지비 −%d · 예산 [b]%d[/b] (이번 주 영업 손익 %+d)" % [roundi(wages), pay_note, roundi(upkeep()), roundi(money), roundi(week_profit())])
	if not shift_violations.is_empty():
		lines.append("[color=#e98]근로시간 위반: %s[/color]" % ", ".join(shift_violations))
	if not shift_incidents.is_empty():
		var names: PackedStringArray = []
		for inc in shift_incidents:
			names.append(inc["type"] + (" (중대사고)" if inc["serious"] else ""))
		lines.append("[color=#e98]이번 교대 사고: %s[/color]" % ", ".join(names))
	lines.append("")
	lines.append("[i]박 씨: \"%s\"[/i]" % _park_line())

	# 현장파·숫자파 교대 판정
	if in_audit() and watch_findings < WATCH_CAP:
		if auditor["type"] == "현장파" and night and solo_dark_hours >= 3:
			watch_findings += 1
			_find("혼자 일하거나 불 꺼진 방에서 작업 (%d일차 밤)" % day)
		if auditor["type"] == "숫자파" and reactor_rate > 80 and finished_shift > 0.0:
			watch_findings += 1
			_find("가동률 %d%% 운영 (%d일차)" % [reactor_rate, day])

	# 상태 정리
	for s in staff:
		s.worked_prev = s.worked_now
		s.worked_now = false
		s.transit = 0
		if s.gone():
			continue
		if s.state == "각성":
			s.state = "정상"
			s.stress = 40.0
		elif s.state == "붕괴" and s.stress < 70.0:
			s.state = "정상"
		if (s.state == "실신" or s.state == "부상") and night:
			s.down_days -= 1
			if s.down_days <= 0:
				s.state = "정상"
		if night:
			if s.away > 0:
				s.away -= 1
			if s.leave_days > 0:
				s.leave_days -= 1
			if s.omen > 0 and not s.exposed_tonight:
				s.clean_nights += 1
				if s.clean_nights >= 2:
					s.omen -= 1
					s.clean_nights = 0
		s.room = "off"
	for id in rooms:
		_clear_extra(id)
		rooms[id]["alert"] = false
	pending_pressure.clear()
	auditor_room = ""
	# 정전이 끝나면 꺼졌던 조명이 돌아온다
	outage = 0
	for c in shed:
		if c[0] == "room" and rooms.has(c[1]):
			set_room_light(c[1], true)
		elif c[0] == "corr":
			set_corr_light(c[1], true)
	shed.clear()

	week_ended = false
	if night:
		week_ended = day % WEEK_DAYS == 0
		day += 1
	shift = "주간" if night else "야간"
	hour = 0
	return "\n".join(lines)


func _open_zone(z: String) -> String:
	zones[z]["open"] = true
	zones[z]["reclaiming"] = false
	power_cap += POWER_PER_ZONE
	to_cap += RECLAIM[z]["to"]
	var id := "purify_" + z
	for s in staff:
		if s.room == id:
			s.room = "off"
	rooms.erase(id)
	return "[color=green]%s 탈환 완료. 조명이 하나씩 켜진다. 빈 칸이 열렸다. (정원 +%d, 전력 +%d)[/color]" % [ZONE_DEF[z]["name"], RECLAIM[z]["to"], POWER_PER_ZONE]


func _find(note: String) -> void:
	findings += 1
	finding_notes.append(note)


## 보고서 결재. conceal이면 이번 교대 사고를 빼고 올린다. 중대사고를 보고하면 조사 결과 글을 돌려준다.
func file_report(conceal: bool) -> String:
	var out := ""
	if shift_incidents.is_empty():
		return out
	if conceal:
		for inc in shift_incidents:
			hidden.append(inc)
		log_line.emit("[color=gray]사고 %d건을 보고서에서 뺐다.[/color]" % shift_incidents.size())
	else:
		week_reported += shift_incidents.size()
		for inc in shift_incidents:
			if in_audit():
				_find("보고된 사고: %s" % inc["type"])
				if inc["override"]:
					_find("작업중지 요구를 무시한 사고")
			elif audit_stage == 2:
				grace_incidents += 2 if inc["override"] else 1
		for inc in shift_incidents.duplicate():
			if inc["serious"]:
				out += _investigate(inc)
	shift_incidents.clear()
	return out


## 중대사고 조사: 안전 의무를 다했으면 면책, 아니면 특별감사로 직행
func _investigate(inc: Dictionary) -> String:
	serious_count += 1
	var lines: Array[String] = []
	lines.append("[font_size=20][b]중대사고 조사[/b][/font_size]")
	lines.append("")
	lines.append("%d일차 %s. 노동청 조사관이 안전 의무 이행 기록을 확인했다." % [inc["day"], inc["type"]])
	var f := 3
	if risk_week or risk_last_week:
		f -= 1
		lines.append("    · [color=green]위험성평가 실시 기록 있음[/color]")
	else:
		lines.append("    · [color=#d55]최근 위험성평가 기록 없음[/color] (QC실에 한 교대 배치하면 그 주에 인정)")
	if viol_week + viol_last_week == 0:
		f -= 1
		lines.append("    · [color=green]근로시간 위반 없음[/color]")
	else:
		lines.append("    · [color=#d55]근로시간 위반 %d건[/color]" % (viol_week + viol_last_week))
	if inc["override"]:
		lines.append("    · [color=#d55]작업중지 요구를 받아들이지 않았다[/color]")
		f += 1
	elif inc["kind"] == "현장":
		f -= 1
		lines.append("    · [color=green]작업중지 요구를 막은 기록 없음[/color]")
	else:
		lines.append("    · [color=#d55]이상 행동 경고가 두 번 나온 직원을 밤에 혼자 두었다[/color]")
	lines.append("")
	if f <= 1:
		lines.append("[color=green]결과: 면책. 안전 의무를 다한 것으로 인정됐다.[/color]")
	else:
		lines.append("[color=#d55]결과: 책임 인정. 사업장 명칭과 재해 내용이 공표된다.[/color]")
		_trust(-15, lines, "중대사고 책임")
		disgraced = true
		lines.append("다음 채용 공고의 지원자가 줄어든다.")
		if audit_stage == 2:
			grace_incidents += 2
			lines.append("진행 중인 특별감사 유예에 사고 기록 2건이 더해진다.")
		else:
			_enter_grace(lines, "중대사고")
			grace_fresh = true
	return "\n".join(PackedStringArray(lines)) + "\n\n"


## 오염 관리: 휴직 (오염 50 이상)
func can_leave(s: Staff) -> bool:
	return not s.gone() and s.leave_days == 0 and s.contamination >= 50.0


func start_leave(s: Staff) -> void:
	if not can_leave(s):
		return
	s.leave_days = LEAVE_DAYS
	s.room = "off"
	log_line.emit("%s에게 %d일 휴직을 줬다. 쉬는 동안 오염이 빠진다." % [s.name, LEAVE_DAYS])


## 오염 관리: 정밀 검진·정화 (오염 25 이상, 즉시)
func can_purify_staff(s: Staff) -> bool:
	return not s.gone() and s.contamination >= 25.0 and money >= PURIFY_STAFF_COST


func purify_staff(s: Staff) -> void:
	if not can_purify_staff(s):
		return
	money -= PURIFY_STAFF_COST
	s.add_contamination(-PURIFY_STAFF_AMOUNT)
	if s.has_trait("변이") and s.contam_stage() < 4:
		s.traits.remove_at(s.traits.find("변이"))
		log_line.emit("%s의 눈빛이 돌아왔다." % s.name)
	log_line.emit("%s 정밀 검진·정화. 오염 −%d. (−%d)" % [s.name, PURIFY_STAFF_AMOUNT, PURIFY_STAFF_COST])


func correct_reports() -> void:
	if hidden.is_empty() or in_audit():
		return
	hidden.clear()
	pre_findings += 1
	log_line.emit("숨겼던 사고를 정정 보고했다. 다음 감사에서 지적 1건으로 처리된다.")


func buy_intel() -> void:
	if audit_stage != 2 or special_known or records < RECORDS_FOR_INTEL:
		return
	records -= RECORDS_FOR_INTEL
	special_known = true
	log_line.emit("관찰 기록을 정리해 넘겼다. 특별감사관은 %s라고 한다." % special_type)


func submit_docs() -> void:
	if audit_stage != 2 or intel_doc or records < RECORDS_FOR_INTEL:
		return
	records -= RECORDS_FOR_INTEL
	intel_doc = true
	log_line.emit("관찰 기록으로 감사 대응 자료를 만들었다. (특별감사 지적 −1)")


# ---------------------------------------------------------------- 주간 결산과 감사

func _trust(d: int, lines: Array[String], why: String) -> void:
	var before := trust
	trust = clampi(trust + d, 0, 100)
	lines.append("본사 신뢰도 %+d (%s) → %d" % [trust - before, why, trust])


func _new_auditor(t: String, nm: String, special: bool, regular: bool) -> void:
	auditor = {"type": t, "name": nm, "special": special, "regular": regular}
	double_stamps = 0
	findings = 0
	watch_findings = 0
	finding_notes.clear()


func _random_auditor_name() -> String:
	return AUDITOR_NAMES[rng.randi() % AUDITOR_NAMES.size()]


func _enter_grace(lines: Array[String], origin: String) -> void:
	audit_stage = 2
	grace_origin = origin
	auditor = {}
	grace_left = GRACE_WEEKS
	grace_fresh = false
	grace_profit_streak = 0
	grace_safe = true
	grace_incidents = 0
	grace_deficits = 0
	intel_doc = false
	special_known = false
	special_type = AUDITOR_TYPES[rng.randi() % AUDITOR_TYPES.size()]
	lines.append("[color=#d55]%d주 뒤 특별감사가 온다.[/color]" % GRACE_WEEKS)
	lines.append("대비: %s · 감사일에 핵심 인력을 쉬게 해두기 · 숨긴 사고 정정 보고 · 가동률 80%% 이하 무사고 · 관찰 기록 활용" % (
		"2주 연속 흑자면 취소" if origin == "적자" else "흑자를 내면 기준 완화"))


## 야간 교대가 끝나 주가 바뀌었을 때 호출. 결산 보고서 글을 돌려준다.
func settle_week() -> String:
	week_ended = false
	var reported_last_week := week_reported
	week_reported = 0
	var profit := week_profit()
	var capex := capex_week
	capex_week = 0.0
	week_start_money = money
	week_history.append(profit)
	var wk := week_history.size()
	var lines: Array[String] = []  # 하위 함수에서 덧붙이므로 참조로 넘어가는 Array를 쓴다
	lines.append("[font_size=20][b]%d주차 주간 결산[/b][/font_size]" % wk)
	lines.append("")
	lines.append("이번 주 영업 손익 [b]%+d[/b] · 예산 %d" % [roundi(profit), roundi(money)])
	if capex > 0.0:
		lines.append("[color=gray]건설·탈환·설치 투자 %d은 손익에서 뺐다. 본사는 투자를 적자로 보지 않는다.[/color]" % roundi(capex))
	if profit >= 0.0:
		_trust(TRUST_PROFIT, lines, "흑자")
	else:
		_trust(-TRUST_DEFICIT, lines, "적자")
	if viol_week > 0:
		lines.append("이번 주 근로시간 위반 %d건. 장부에 남아 다음 감사 때 지적이 된다." % viol_week)
	lines.append("")

	if perk_repair_weeks > 0:
		perk_repair_weeks -= 1
	if perk_upkeep_weeks > 0:
		perk_upkeep_weeks -= 1
	viol_last_week = viol_week
	viol_week = 0
	risk_last_week = risk_week
	risk_week = false
	ext_week = false
	for s in staff:
		s.shifts_week = 0

	var judged := false
	match audit_stage:
		1:
			judged = true
			var regular: bool = auditor.get("regular", false)
			var ok := _judge(lines)
			audits_held += 1
			if regular:
				regular_held += 1
			if ok:
				_reward(lines)
				if regular:
					to_cap += 1
					_trust(10, lines, "정기감사 통과")
					lines.append("정원 +1 (현재 %d)" % to_cap)
				else:
					_trust(5, lines, "감사 통과")
				audit_stage = 0
				auditor = {}
			else:
				lines.append("[color=#d55]감사 불통과.[/color]")
				_trust(-10, lines, "감사 불통과")
				_enter_grace(lines, "정기" if regular else "적자")
		2:
			if grace_fresh:
				grace_fresh = false
				lines.append("특별감사까지 %d주." % grace_left)
			else:
				grace_left -= 1
				if profit >= 0.0:
					grace_profit_streak += 1
				else:
					grace_profit_streak = 0
					grace_deficits += 1
				if grace_left > 0:
					lines.append("특별감사까지 %d주. 흑자 연속 %d주." % [grace_left, grace_profit_streak])
				elif grace_profit_streak >= GRACE_WEEKS and grace_origin == "적자":
					audit_stage = 0
					lines.append("[color=green]%d주 연속 흑자. 본사가 특별감사를 취소했다.[/color]" % GRACE_WEEKS)
					_trust(5, lines, "특별감사 취소")
				else:
					judged = true
					_special_audit(lines)

	# 정기감사 예고 (2주 전)
	if REGULAR_WEEKS.has(wk + 2):
		regular_next = {"week": wk + 2, "type": AUDITOR_TYPES[rng.randi() % AUDITOR_TYPES.size()], "name": _random_auditor_name()}
		lines.append("")
		lines.append("[color=#e98]정기감사 예고: %d주차에 %s %s이(가) 온다.[/color]" % [wk + 2, regular_next["type"], regular_next["name"]])
		lines.append("주로 보는 것: %s" % AUDITOR_INFO[regular_next["type"]]["watch"])
		lines.append("숨긴 사고와 근로시간 장부를 전부 본다. 통과하면 신뢰도 +10, 정원 +1, %s" % AUDITOR_INFO[regular_next["type"]]["reward"])

	# 다음 주에 감사가 시작되는가
	if replaced or day > WIN_DAY:
		return "\n".join(PackedStringArray(lines))
	var next_week := wk + 1
	var regular_due: bool = not regular_next.is_empty() and regular_next["week"] == next_week
	if audit_stage == 0:
		if regular_due:
			lines.append("")
			_start_regular(lines, profit < 0.0, reported_last_week)
		elif profit < 0.0:
			lines.append("")
			if judged:
				lines.append("감사 직후라 이번 적자는 경고로 넘어간다.")
			elif not regular_next.is_empty():
				deficit_carry += 1
				lines.append("적자. 본사는 따로 감사를 보내지 않고 %d주차 정기감사에서 함께 보기로 했다." % regular_next["week"])
			else:
				_start_first_audit(lines, reported_last_week)
		elif not judged:
			lines.append("본사: 이상 없음.")
	elif audit_stage == 2 and regular_due:
		regular_next = {}
		lines.append("")
		lines.append("다음 주 정기감사는 특별감사로 갈음한다.")
	return "\n".join(PackedStringArray(lines))


func _start_first_audit(lines: Array[String], reported_last_week: int) -> void:
	audit_stage = 1
	_new_auditor(AUDITOR_TYPES[rng.randi() % AUDITOR_TYPES.size()], _random_auditor_name(), false, false)
	lines.append("[color=#e98]적자. 다음 주 본사 감사가 온다: %s %s[/color]" % [auditor["type"], auditor["name"]])
	lines.append(AUDITOR_INFO[auditor["type"]]["arrive"])
	lines.append("주로 보는 것: %s" % AUDITOR_INFO[auditor["type"]]["watch"])
	lines.append("통과하면: %s" % AUDITOR_INFO[auditor["type"]]["reward"])
	_carry_reported(lines, reported_last_week)


func _start_regular(lines: Array[String], deficit: bool, reported_last_week: int) -> void:
	audit_stage = 1
	_new_auditor(regular_next["type"], regular_next["name"], false, true)
	regular_next = {}
	lines.append("[color=#e98]정기감사 시작: %s %s (1주 상주)[/color]" % [auditor["type"], auditor["name"]])
	lines.append(AUDITOR_INFO[auditor["type"]]["arrive"])
	lines.append("주로 보는 것: %s" % AUDITOR_INFO[auditor["type"]]["watch"])
	if deficit:
		deficit_carry += 1
		_carry_reported(lines, reported_last_week)
	for i in mini(deficit_carry, 2):
		_find("정기감사 전 적자 주")
	if deficit_carry > 0:
		lines.append("감사 전 적자 %d주가 지적으로 잡혔다." % mini(deficit_carry, 2))
	deficit_carry = 0


func _carry_reported(lines: Array[String], reported_last_week: int) -> void:
	var carry := mini(reported_last_week, LAST_WEEK_CAP)
	for i in carry:
		_find("지난주 보고된 사고")
	if carry > 0:
		lines.append("지난주 보고서의 사고 기록으로 지적 %d건을 안고 시작한다." % carry)


## 특별감사일: 유예가 끝난 결산 다음 날 아침 하루
func _special_audit(lines: Array[String]) -> void:
	specials_held += 1
	relaxed = grace_profit_streak >= 1
	_new_auditor(special_type, _random_auditor_name(), true, false)
	aces = 0
	for s in staff:
		if s.can_work() and s.operation >= 55.0 and s.fatigue < 40.0 and s.stress < 60.0:
			aces += 1
	aces = mini(aces, 1)
	lines.append("[color=#e98]특별감사일: %s %s[/color]" % [auditor["type"], auditor["name"]])
	lines.append(AUDITOR_INFO[auditor["type"]]["arrive"])
	lines.append("쉬고 나온 대응 인력 %d명%s" % [aces, " · 흑자 반영: 기준 완화" if relaxed else ""])
	for i in grace_incidents:
		_find("유예 기간 사고 기록")
	for i in grace_deficits:
		_find("유예 기간 적자 주")
		_find("유예 기간 적자 주 (개선 없음)")
	var ok := _judge(lines)
	auditor = {}
	if ok:
		audit_stage = 0
		lines.append("[color=green]특별감사 통과. 공장장 자리를 지켰다.[/color]")
		_trust(5, lines, "특별감사 통과")
	else:
		replaced = true
		lines.append("[color=#d55]특별감사 불통과. 본사가 공장장 교체를 결정했다.[/color]")


func _judge(lines: Array[String]) -> bool:
	var t: String = auditor["type"]
	var kept: Array = []
	for inc in hidden:
		if rng.randf() < DETECT[t][inc["kind"]]:
			if inc["serious"]:
				findings += 3
				finding_notes.append("숨긴 중대사고 적발: %s (%d일차) — 가중" % [inc["type"], inc["day"]])
			else:
				findings += 2
				finding_notes.append("숨긴 사고 적발: %s (%d일차) — 가중" % [inc["type"], inc["day"]])
		else:
			kept.append(inc)
	hidden = kept
	if pre_findings > 0:
		findings += pre_findings
		finding_notes.append("정정 보고한 사고")
		pre_findings = 0
	if violations_open > 0:
		var v := mini(violations_open, LEDGER_CAP[t])
		findings += v
		finding_notes.append("근로시간 위반 %d건 중 %d건 지적" % [violations_open, v])
		violations_open = 0
	if money < 0.0:
		_find("예산 마이너스 (재무 건전성)")
	if double_stamps >= DOUBLE_STAMP_MIN and LEDGER_CAP[t] >= 2 and not auditor["special"]:
		_find("출근 기록이 두 번씩 찍힌 직원 (%d교대, 설명 불가)" % double_stamps)
	var limit := SPECIAL_PASS_MAX if auditor["special"] else AUDIT_PASS_MAX
	if auditor["special"]:
		var credit := aces
		if grace_safe:
			credit += 1
		if intel_doc:
			credit += 1
		if relaxed:
			limit += 1
		if credit > 0:
			finding_notes.append("대비 반영: 지적 −%d" % credit)
		findings = maxi(0, findings - credit)
	var kind := "특별감사" if auditor["special"] else ("정기감사" if auditor.get("regular", false) else "1차 감사")
	lines.append("[b]%s %s %s 결과: 지적 %d건 (통과 기준 %d건 이하)[/b]" % [kind, t, auditor["name"], findings, limit])
	for n in finding_notes:
		lines.append("    · " + n)
	audit_log.append([t, kind, findings, findings <= limit, finding_notes.duplicate()])
	return findings <= limit


func _reward(lines: Array[String]) -> void:
	var t: String = auditor["type"]
	lines.append("[color=green]감사 통과.[/color] 보상: %s" % AUDITOR_INFO[t]["reward"])
	match t:
		"서류파":
			money += 200.0
			week_start_money += 200.0
		"현장파":
			perk_repair_weeks = 2
		"숫자파":
			perk_upkeep_weeks = 2


func _delta(a: float, b: float) -> String:
	var d := roundi(b - a)
	var mark := "+" if d >= 0 else "−"
	return "%d(%s%d)" % [roundi(b), mark, absi(d)]


func _park_line() -> String:
	if vanished_shift > 0:
		return "다들 무사히 돌아왔네요. 대부분은."
	if in_audit():
		return "본사 양반이 와 있으니 다들 조심하세요. 그 사람도요."
	if is_night() and anomaly_count_shift == 0:
		return "조용한 밤이었네요. 이런 날도 있어야죠."
	var pool := [
		"밤을 넘기는 게 이기는 거라고, 우리 어머니가 그랬어요.",
		"창고 쪽은 불 켜두세요. 옛날부터 그랬어요.",
		"이번 공장장님은 오래 계셨으면 좋겠네.",
		"사람 수는 두 번 세는 게 좋아요. 그냥 습관이에요.",
		"복도는 둘이 걸으세요. 셋이 되면 뛰시고.",
	]
	return pool[rng.randi() % pool.size()]


func is_victory() -> bool:
	return defeat_reason() == "" and day > WIN_DAY


func defeat_reason() -> String:
	if replaced:
		return "교체"
	if trust <= 0:
		return "해임"
	if alive_count() < MIN_STAFF:
		return "인원"
	return ""


func is_defeat() -> bool:
	return defeat_reason() != ""
