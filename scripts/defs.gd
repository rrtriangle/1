class_name Defs
extends RefCounted
## 수치와 맵 정의. 밸런스는 여기서만 바꾼다. 전부 [제안] 값이다.

# ---------------------------------------------------------------- 시간
const HOUR_SEC := 40.0          # 1배속에서 게임 1시간 = 40초 (한 교대 8분)
const SHIFT_MIN := 720          # 한 교대 12시간
const DEMO_LAST_DAY := 10       # 프롤로그 3일 + 1주차. 이 날 밤이 끝나면 데모 종료
const PROLOGUE_DAYS := 3
const PROTECT_DAY := 7          # 이 날까지는 실종이 없다 (원칙 5)
const WEEK_DAYS := 7

# ---------------------------------------------------------------- 돈
const START_MONEY := 1500.0
const SELL_PRICE := 65.0        # 세정액 드럼 1개
const RAW_PRICE := 20.0         # 원료 드럼 1개
const WAGE_PER_SHIFT := 20.0    # 직원 1명, 교대 1번 (근무 여부와 관계없이 월급으로 나간다)
const UPKEEP_PER_SHIFT := 50.0  # 공장 유지비
const CALL_PAY := 40.0          # 대타 수당 (붙잡기·호출 1번)
const REPAIR_COST := 200.0      # 역압 폭발 수리비
const RUSH_PRICE := 80.0        # 긴급 주문 단가
const RUSH_DRUMS := 10
const RUSH_PENALTY := 150.0

# ---------------------------------------------------------------- 생산
const TANK_CAP := 40.0
const BUFFER_CAP := 8.0         # 반응실 → 정제실 사이 미정제 탱크
const OUTBOX_CAP := 4.0         # 정제실 출구. 창고 직원이 안 옮기면 여기서 막힌다
const STORE_CAP := 60.0
const REFINE_RATE := 1.2        # 정제실 직원 1명(숙련 60)의 시간당 처리량
const HAUL_RATE := 4.0          # 창고 직원 1명이 시간당 옮기는 드럼
const SECOND_WORKER := 0.5      # 같은 방 두 번째 사람의 기여
const DARK_WORK := 0.6          # 불 꺼진 방의 작업 효율
const RAW_DELIVERY_T := 30      # 주간 08:30 원료 입고
const TRUCK_T := [120, 480]     # 주간 10:00, 16:00 트럭
const TRUCK_CAP := 15

# ---------------------------------------------------------------- 사람
const WALK_PX_PER_MIN := 22.0   # 맵 가로를 건너는 데 약 1시간
const CALL_TRAVEL_MIN := 40     # 호출받은 사람이 집에서 정문까지 오는 시간 (평균)
const CALL_TRAVEL_SPREAD := 20  # ±. 집이 먼 사람, 잠이 덜 깬 사람
const FATIGUE_WORK_H := 2.5     # 근무 중 시간당 피로
const FATIGUE_NIGHT_H := 3.0
const FATIGUE_REST_SHIFT := 25.0  # 쉬는 교대마다 회복
const FATIGUE_SLOW := 70.0      # 이 이상이면 능력 −20%
const STRESS_BREAK := 100.0
const HOLD_FATIGUE := 15.0      # 붙잡기: 이미 12시간 일한 사람이 더 일한다

# ---------------------------------------------------------------- 이상현상
const PRESSURE_K := 0.04        # 역압: 시간당 확률 = K × ((가동률 − 80) ÷ 20)²
const PRESSURE_NIGHT := 1.5
const PRESSURE_DAY := 0.4
const PRESSURE_WINDOW := 75     # 전조에서 폭발까지 (분). 반장은 제때 오고, 호출은 아슬아슬하다
const EXTRA_ALONE := 0.02       # 한 명 더: 밤에 혼자 있는 방, 시간당
const EXTRA_DARK := 0.05        # 불 꺼진 방이면 더한다
const EXTRA_STAGE3 := 0.04      # 검은 무늬(오염 75+) 직원이 있으면 더한다
const EXTRA_REPORT_DELAY := 40  # 나타난 뒤 무전이 오기까지 (분)
const OUTAGE_NIGHT := 0.035
const OUTAGE_DAY := 0.01
const FIX_LIGHT_MIN := 15
const PERFECT_P := 0.2          # 직접 대응이 성공했을 때 ★ 완벽한 차단 확률
const RADIO_GAP := 25           # 무전 사이 최소 간격 (분)

# ---------------------------------------------------------------- 연구
const RESEARCH_STAGES := {"extra": [6, 15, 26], "pressure": [5, 12, 21]}  # 단계별 누적 관찰 기록
const RESEARCH_NAMES := {"extra": "한 명 더", "pressure": "역압"}

# ---------------------------------------------------------------- 설비 투자 (돈을 쓰는 곳)
const SHOP := {
	"em": {"name": "비상 조명", "cost": 120, "what": "방 하나. 정전이 와도 그 방 불은 안 꺼진다"},
	"party": {"name": "회식", "cost": 200, "what": "전원 스트레스 −20. 한 주에 한 번"},
}
const EM_ROOMS := ["reactor", "refine", "store", "tank", "dock"]

# ---------------------------------------------------------------- 맵 (맵 화면 좌표, 1140×930)
const MAP_SIZE := Vector2(1140, 930)
const HALL_Y := [300.0, 660.0]  # 위 복도, 아래 복도
const LINK_X := [25.0, 625.0]  # 두 복도를 잇는 통로 (왼쪽 끝, 가운데)
const ROOMS := {
	"lab": {"name": "연구소", "rect": Rect2(50, 30, 230, 220), "hall": 0,
		"does": "관찰 기록을 연구해 대처법(근무 수칙)을 찾는다", "who": "연구원 (낮)"},
	"office": {"name": "관리실", "rect": Rect2(300, 30, 200, 220), "hall": 0,
		"does": "주문·납기·원료를 처리한다", "who": "관리 사원 (낮)"},
	"maint": {"name": "정비실", "rect": Rect2(520, 30, 190, 220), "hall": 0,
		"does": "고장을 고치고 설비를 점검한다", "who": "정비원 (낮)"},
	"rest": {"name": "휴게실", "rect": Rect2(730, 30, 200, 220), "hall": 0,
		"does": "쉬면 피로가 빨리 풀린다", "who": "누구나"},
	"tank": {"name": "원료 탱크", "rect": Rect2(50, 350, 130, 260), "hall": 1,
		"does": "주간 08:30에 원료가 들어온다 (드럼당 20)", "who": "-"},
	"reactor": {"name": "반응실", "rect": Rect2(200, 350, 230, 260), "hall": 1,
		"does": "원료를 반응시켜 미정제 케미컬을 만든다", "who": "1~2명"},
	"control": {"name": "조정실", "rect": Rect2(450, 350, 140, 260), "hall": 1,
		"does": "반장이 무전을 받고 지시한다. CCTV가 있어 혼자여도 안전하다", "who": "반장"},
	"refine": {"name": "정제실", "rect": Rect2(670, 350, 220, 260), "hall": 1,
		"does": "미정제를 세정액 드럼으로 정제한다", "who": "1~2명"},
	"store": {"name": "창고", "rect": Rect2(910, 350, 210, 260), "hall": 1,
		"does": "정제실 드럼을 옮겨 쌓는다. 비면 정제실이 막힌다", "who": "1명"},
	"gate": {"name": "정문·경비실", "rect": Rect2(50, 710, 250, 200), "hall": 1,
		"does": "박 씨가 출입을 기록한다. 출퇴근은 여기로", "who": "박 씨"},
	"dock": {"name": "출하장", "rect": Rect2(870, 710, 250, 200), "hall": 1,
		"does": "주간 10시·16시 트럭이 드럼을 싣고 간다 (드럼당 65)", "who": "-"},
}
const PRODUCTION := ["reactor", "refine", "store"]
const PATROL_ROUTE := ["reactor", "tank", "refine", "store", "dock", "control"]
const POST_NAMES := {
	"control": "조정실 (지휘)", "reactor": "반응실", "refine": "정제실", "store": "창고",
	"patrol": "순찰", "assist": "정제실 보조", "rest": "휴게실 (쉼)",
	"lab": "연구소", "maint": "정비실", "office": "관리실",
}

# ---------------------------------------------------------------- 사람 (시작 18명)
# skill 숙련, observe 관찰, nerve 담력. 0~100.
const ROSTER := [
	{"name": "김 반장", "role": "반장", "crew": 1, "skill": 85, "observe": 40, "nerve": 60, "traits": ["고집불통"], "age": 52},
	{"name": "오 기사", "role": "조원", "crew": 1, "skill": 66, "observe": 50, "nerve": 55, "traits": ["꼼꼼함"], "age": 41},
	{"name": "정 사원", "role": "조원", "crew": 1, "skill": 45, "observe": 72, "nerve": 40, "traits": ["호기심 많음"], "age": 27},
	{"name": "하 사원", "role": "조원", "crew": 1, "skill": 38, "observe": 50, "nerve": 50, "traits": ["신입"], "age": 24},
	{"name": "이 주임", "role": "조원", "crew": 1, "skill": 60, "observe": 45, "nerve": 45, "traits": [], "age": 36},
	{"name": "윤 반장", "role": "반장", "crew": 2, "skill": 72, "observe": 55, "nerve": 65, "traits": ["안전 제일"], "age": 47},
	{"name": "서 기사", "role": "조원", "crew": 2, "skill": 62, "observe": 48, "nerve": 58, "traits": [], "age": 39},
	{"name": "장 주임", "role": "조원", "crew": 2, "skill": 58, "observe": 52, "nerve": 35, "traits": ["잠을 못 잠"], "age": 34},
	{"name": "고 사원", "role": "조원", "crew": 2, "skill": 44, "observe": 60, "nerve": 52, "traits": [], "age": 29},
	{"name": "노 사원", "role": "조원", "crew": 2, "skill": 40, "observe": 45, "nerve": 62, "traits": ["신입"], "age": 25},
	{"name": "최 반장", "role": "반장", "crew": 3, "skill": 60, "observe": 62, "nerve": 55, "traits": ["신참 반장"], "age": 38},
	{"name": "임 기사", "role": "조원", "crew": 3, "skill": 70, "observe": 42, "nerve": 60, "traits": ["줄담배"], "age": 45},
	{"name": "구 사원", "role": "조원", "crew": 3, "skill": 48, "observe": 58, "nerve": 48, "traits": [], "age": 30},
	{"name": "도 사원", "role": "조원", "crew": 3, "skill": 42, "observe": 66, "nerve": 42, "traits": ["예민함"], "age": 26},
	{"name": "탁 기사", "role": "조원", "crew": 3, "skill": 64, "observe": 40, "nerve": 66, "traits": [], "age": 43},
	{"name": "한 연구원", "role": "연구원", "crew": 0, "skill": 40, "observe": 78, "nerve": 45, "traits": [], "age": 33},
	{"name": "문 정비원", "role": "정비원", "crew": 0, "skill": 72, "observe": 45, "nerve": 58, "traits": [], "age": 44},
	{"name": "배 사원", "role": "관리", "crew": 0, "skill": 50, "observe": 50, "nerve": 50, "traits": [], "age": 28},
]
const STAFF_POST := {"연구원": "lab", "정비원": "maint", "관리": "office"}
const TRAIT_INFO := {
	"고집불통": "지시를 15% 확률로 \"괜찮아요\" 하고 안 따른다. 스트레스에 강하다",
	"안전 제일": "위기 때 먼저 \"세우자\"고 한다. 가동 중지 지시는 반드시 따른다",
	"꼼꼼함": "실수가 적어 대응 성공률 +5%p, 작업은 조금 느리다",
	"호기심 많음": "관찰 기록을 더 잘 얻지만, 이상한 것에 먼저 다가간다",
	"신입": "아직 서툴다. 스트레스를 더 받는다",
	"잠을 못 잠": "요즘 잠을 못 잔다고 한다",
	"신참 반장": "반장이 된 지 얼마 안 됐다. 조원들이 아직 덜 따른다",
	"줄담배": "가끔 담배 피우러 자리를 비운다",
	"예민함": "이상한 것을 잘 알아채지만 스트레스를 더 받는다",
}

# 관계: [사람, 사람, 관계]. 사수·부사수는 같은 조에서 서로 의지하고, 앙숙은 같은 방이면 서로 지친다.
const RELATIONS := [
	["김 반장", "하 사원", "사수"],
	["오 기사", "이 주임", "앙숙"],
	["정 사원", "고 사원", "동기"],
	["윤 반장", "노 사원", "사수"],
	["서 기사", "장 주임", "동기"],
	["임 기사", "최 반장", "앙숙"],
	["구 사원", "도 사원", "동기"],
	["탁 기사", "임 기사", "동기"],
]

# 방에 놓는 설비 (낮에 맵 아래 설비 막대에서 골라 방을 누른다)
const EQUIP := {
	"em": {"name": "비상 조명", "cost": 120, "rooms": ["reactor", "refine", "store", "tank", "dock"],
		"what": "정전이 와도 이 방 불은 안 꺼진다"},
	"cam": {"name": "CCTV", "cost": 120, "rooms": ["reactor", "refine", "store", "tank", "dock"],
		"what": "조정실에서 이 방 인원을 센다. '인원 불일치'가 바로 뜨고, 불이 꺼져도 안이 보인다"},
	"heater": {"name": "난로", "cost": 60, "rooms": ["store", "tank", "dock"],
		"what": "밤에 추워서 쌓이는 피로·스트레스가 없다"},
	"lock": {"name": "비상문 잠금", "cost": 80, "rooms": ["store", "tank", "dock"],
		"what": "비상문이 저절로 열리지 않는다. '한 명 더' 확률 −1%p"},
	"chair": {"name": "안마의자", "cost": 200, "rooms": ["rest"],
		"what": "휴게실에서 피로가 두 배로 풀린다"},
	"coffee": {"name": "커피 자판기", "cost": 100, "rooms": ["control"],
		"what": "야간조 피로가 시간당 0.5 덜 쌓인다"},
}

# 근무표: 6일 주기 (주간, 주간, 야간, 야간, 휴무, 휴무). 조마다 2일씩 어긋난다.
const CYCLE := ["day", "day", "night", "night", "off", "off"]
const CREW_OFFSET := {1: 0, 2: 2, 3: 4}


static func duty_of(crew: int, day: int) -> String:
	return CYCLE[(day - 1 + CREW_OFFSET[crew]) % 6]


static func crew_on(day: int, shift: String) -> int:
	for c in [1, 2, 3]:
		if duty_of(c, day) == shift:
			return c
	return 1


static func room_center(id: String) -> Vector2:
	var r: Rect2 = ROOMS[id]["rect"]
	return r.get_center()


static func door_point(id: String) -> Vector2:
	var r: Rect2 = ROOMS[id]["rect"]
	var h: int = ROOMS[id]["hall"]
	return Vector2(r.get_center().x, HALL_Y[h])


## from 위치에서 방 to까지 걸어가는 경로 (복도를 따라간다)
static func path_to(from_pos: Vector2, from_room: String, to: String) -> Array[Vector2]:
	var out: Array[Vector2] = []
	var h_from: int = 1
	var x_from := from_pos.x
	if from_room != "" and ROOMS.has(from_room):
		h_from = ROOMS[from_room]["hall"]
		x_from = room_center(from_room).x
	else:
		# 복도 위에 있는 경우: 가까운 복도를 쓴다
		h_from = 0 if absf(from_pos.y - HALL_Y[0]) < absf(from_pos.y - HALL_Y[1]) else 1
	var h_to: int = ROOMS[to]["hall"]
	out.append(Vector2(x_from, HALL_Y[h_from]))
	if h_from != h_to:
		# 가까운 통로로 건너간다
		var x_to := room_center(to).x
		var link: float = LINK_X[0]
		for lx in LINK_X:
			if absf(x_from - lx) + absf(lx - x_to) < absf(x_from - link) + absf(link - x_to):
				link = lx
		out.append(Vector2(link, HALL_Y[h_from]))
		out.append(Vector2(link, HALL_Y[h_to]))
	out.append(door_point(to))
	out.append(room_center(to))
	return out
