class_name Story
extends RefCounted
## 이야기: 왜 이 공장을 지켜야 하는가.
## 전임자 메모(수수께끼), 직원 이야기 줄기(지켜야 할 사람), 본사 주간 목표(지켜야 할 자리),
## 회의의 "오늘 밤 걱정거리", 데모 엔딩. 규칙은 sim.gd, 여기는 내용과 그 연결만.

var sim: Sim
var memos_found: Array = []      # 찾은 메모 id 순서대로
var flags := {}                  # 이야기 분기
var fired := {}                  # 이미 나온 장면
var park_hint_room := ""         # 박 씨가 "불 켜 두라"고 한 방 (그날 밤 그 방이 위험하다)
var park_hint_true := false

const OPENING := "[b]의산공장 공장장 발령[/b]\n\n전임 공장장은 지난주 야간 근무 중 연락이 끊겼습니다. 사직서도, 인수인계도 없었습니다.\n본사는 당신을 보냈습니다. 조건은 둘입니다.\n\n  · 매주 본사가 정한 출하량을 맞출 것\n  · 사람을 잃지 말 것\n\n공장장실 서랍에는 전임자가 남긴 메모 몇 장과, 다 쓰지 못한 인수인계서가 있습니다."

## 전임자 메모. 찾을 때마다 수첩에 쌓인다. 규칙의 힌트이자 이야기 조각.
const MEMOS := {
	"m1": {"title": "서랍 속 첫 장", "text": "박 씨 얘기는 끝까지 들어.\n(그 아래, 다른 펜으로) 끝까지 들은 다음에 판단해."},
	"m2": {"title": "압력계 옆 포스트잇", "text": "숫자 믿지 마.\n계기가 올라가면 실제로는 내려가는 중이다. 반대로 해."},
	"m3": {"title": "흔적 조사 중 발견", "text": "검은 자국은 걸어간 방향이 아니라 걸어온 방향으로 끊긴다.\n어디서 왔는지 따라가 보지 말 것."},
	"m4": {"title": "출근 명단판 뒤", "text": "혼자 두지 마. 둘이면 안 온다.\n셋이면 셋 다 서로를 한 번씩 세어 볼 것."},
	"m5": {"title": "경비실 창틀에 끼워져 있던 것", "text": "박 씨가 사람을 보내 달라고 하면\n(여기서부터 찢겨 있다)"},
	"m6": {"title": "연구소 캐비닛", "text": "연구는 해야 한다. 하지만 다 알게 되면 그게 우리를 알게 된다.\n3단계에서 멈춘 사람이 나 말고도 있었다."},
	"m7": {"title": "경비 일지 사본", "text": "박 씨가 틀린 날을 적어 둘 것.\n틀린 게 아니라 '아직'인 날일 수도 있다."},
	"m8": {"title": "인수인계서 마지막 장", "text": "이 문서를 처음 읽는 게 아니라면, 3번 항목부터 다시 읽으세요.\n3. 출근 명단에 같은 이름이 두 번 있으면, 먼저 찍힌 쪽이 진짜다."},
}

## 본사 주간 목표: 출하 드럼 수
const WEEK_GOAL := {1: 140, 2: 150}
const GOAL_REWARD := 400.0


func _init(s: Sim) -> void:
	sim = s


# ================================================================ 메모

func find_memo(id: String) -> void:
	if memos_found.has(id):
		return
	memos_found.append(id)
	var m: Dictionary = MEMOS[id]
	sim._new_radio("memo", "전임자 메모 %d/%d" % [memos_found.size(), MEMOS.size()],
		"[%s]\n\n%s" % [m["title"], m["text"]], "", [sim._choice("수첩에 끼워 둔다", "ok")])
	sim.history.append({"day": sim.day, "shift": sim.shift, "t": sim.t, "what": "memo", "kind": id})


func known_lines() -> Array:
	var out: Array = []
	for id in memos_found:
		out.append("「%s」" % MEMOS[id]["text"].split("\n")[0])
	return out


func open_questions() -> Array:
	var q: Array = ["전임 공장장은 어디로 갔나?"]
	if sim.errand_state in ["away", "back"]:
		q.append("%s은(는) 심부름에서 무엇을 하고 왔나?" % sim.errand_person.name)
	if sim.stats["park_right"] + sim.stats["park_wrong"] > 0:
		q.append("박 씨는 어떻게 아는가? (맞음 %d, 틀림 %d)" % [sim.stats["park_right"], sim.stats["park_wrong"]])
	if flags.has("seal_seen"):
		q.append("1호 반응기 봉인문 너머의 불빛은 무엇인가?")
	return q


# ================================================================ 본사 주간 목표

func week_goal() -> int:
	var w := (sim.day - 1) / Defs.WEEK_DAYS + 1
	return WEEK_GOAL.get(w, 150)


func goal_line() -> String:
	return "본사 목표: 이번 주 출하 %d/%d드럼 · 실종 0" % [sim.shipped_week, week_goal()]


## 주간 결산 때 부른다. 결과 문구와 보상.
func judge_week() -> Dictionary:
	var goal := week_goal()
	var ok_ship: bool = sim.shipped_week >= goal
	var ok_people: bool = sim.vanished_week == 0
	var out := {"goal": goal, "shipped": sim.shipped_week, "ok": ok_ship and ok_people}
	if out["ok"]:
		sim.money += GOAL_REWARD
		out["text"] = "본사 목표 달성. 특별 예산 +%d이 내려왔다. 본사 메일: \"계속 이대로만.\"" % GOAL_REWARD
	elif not ok_ship:
		out["text"] = "본사 목표 미달 (%d/%d드럼). 본사 메일: \"다음 주에 감사팀이 갑니다.\" (감사는 v0.7 다음에 만든다)" % [sim.shipped_week, goal]
	else:
		out["text"] = "출하는 맞췄지만 사람을 잃었다. 본사 메일: \"경위서를 제출하세요.\""
	return out


# ================================================================ 직원 이야기 줄기
# 날짜·교대·시각에 맞춰 장면이 온다. 선택에 따라 다음 장면이 갈린다.

func tick() -> void:
	var d := sim.day
	var night := sim.is_night()
	var t := sim.t
	# 1일차 아침: 서랍 속 첫 메모
	if d == 1 and not night and t == 12:
		find_memo("m1")
	# 김 반장: 정년 (2일차 낮)
	if d == 2 and not night and t == 300:
		_scene("kim_retire", "김 반장", "control",
			"공장장님, 저 이번 달 말이 정년입니다. 30년 했어요.\n마지막 한 달은... 우리 조에서 사고 없이 끝내고 싶습니다.",
			[sim._choice("\"약속하죠. 1조는 제가 지킵니다.\"", "promise", -1.0, "김 반장 신뢰 +10. 1조 근무 중 사고가 나면 크게 실망한다"),
			sim._choice("\"장담은 못 합니다.\"", "honest", -1.0, "")])
	# 최 반장: 신참 반장 (3일차 낮)
	if d == 3 and not night and t == 200:
		_scene("choi_rookie", "최 반장", "control",
			"공장장님... 임 기사님이 제 말을 안 들어요. 자기가 20년 했다고, 반응실은 자기 방식대로 한다고.\n제가 반장인데요.",
			[sim._choice("최 반장 편을 든다", "back", -1.0, "최 반장 신뢰 +15. 임 기사 스트레스 +8"),
			sim._choice("임 기사 방식대로 두라고 한다", "vet", -1.0, "최 반장 신뢰 −10. 임 기사는 만족한다")])
	# 하 사원: 첫 야간 (3일차 밤, 1조)
	if d == 3 and night and t == 100:
		var ha := _p("하 사원")
		if ha != null and ha.on_site:
			_scene("ha_first", "하 사원", ha.room,
				"공장장님... 저 야간 처음이에요. %s에 혼자 있으니까 자꾸 뒤에 누가 있는 것 같아요.\n죄송해요. 별거 아닌데." % Defs.ROOMS.get(ha.room, {"name": "여기"})["name"],
				[sim._choice("사수 김 반장이 같이 있어 준다", "kim", -1.0, "하 사원 스트레스 −15. 김 반장이 조정실을 비운다 (그동안 무전이 늦다)"),
				sim._choice("순찰이 그 방을 자주 들르게 한다", "patrol", -1.0, "하 사원 스트레스 −5"),
				sim._choice("\"다들 처음엔 그래요. 버텨요.\"", "endure", -1.0, "하 사원 스트레스 +12")])
	# 하 사원: 두 번째 밤 (4일차 밤)
	if d == 4 and night and t == 240 and not fired.has("ha_second"):
		fired["ha_second"] = true
		var ha2 := _p("하 사원")
		if ha2 != null and ha2.on_site and not ha2.walking():
			if flags.get("ha_supported", false):
				sim._new_radio("story", "하 사원", "공장장님. 창고 구석에 서 있는 사람... 저 사람 명단에 없죠?\n어제 반장님이 알려 주신 대로 세어 봤어요. 여섯이에요.", ha2.room, [sim._choice("\"잘했어요. 바로 사람 보낼게요.\"", "ok")])
				ha2.observe = minf(95.0, ha2.observe + 15.0)
				sim._spawn_entity(ha2.room if ha2.room != "" else "store")
				sim.entity_reported = true
			else:
				ha2.add_stress(40.0, "어젯밤 혼자 버텼다")
				sim._new_radio("story", "김 반장", "하 사원이 %s 구석에 웅크리고 있어요. 말을 안 해요.\n어젯밤부터 그랬던 것 같습니다." % Defs.ROOMS.get(ha2.room, {"name": "방"})["name"], ha2.room, [sim._choice("...", "ok")])
	# 정 사원: 봉인문 (4일차 밤, 1조)
	if d == 4 and night and t == 400:
		var j := _p("정 사원")
		if j != null and j.on_site:
			_scene("jung_seal", "정 사원", j.room,
				"공장장님, 1호 반응기 봉인문 틈으로 불빛이 새요. 거긴 30년 동안 안 열었다면서요?\n잠깐만 보고 올게요. 진짜 잠깐만.",
				[sim._choice("보고 오게 한다", "go", -1.0, "관찰 기록 +3. 정 사원 오염 +20"),
				sim._choice("절대 가지 마라", "no", -1.0, "정 사원 스트레스 +8")])
	# 장 주임(또는 심부름 다녀온 사람): 출근 기록 두 번 (5일차 낮)
	if d == 5 and not night and t == 150 and sim.errand_person != null and sim.errand_state == "back":
		var e := sim.errand_person
		_scene("double_stamp", "배 사원", "office",
			"공장장님, 이상한 게 있어요. %s 출근 기록이 어제 두 번 찍혀 있어요. 20:00, 20:00. 같은 시각에.\n카드는 한 장뿐인데요." % e.name,
			[sim._choice("기계 오류로 정정한다", "fix", -1.0, "기록은 깔끔해진다"),
			sim._choice("그대로 둔다", "keep", -1.0, "나중에 감사가 이 기록을 볼 수 있다")])
	# 메모 발견 조건 (이야기 밖 사건과 연결)
	if not memos_found.has("m7") and sim.stats["park_wrong"] + sim.stats["park_right"] >= 2:
		find_memo("m7")
	if not memos_found.has("m6") and sim.research["extra"] + sim.research["pressure"] >= 1:
		find_memo("m6")


func _p(n: String) -> Person:
	for p in sim.people:
		if p.name == n and not p.gone:
			return p
	return null


func _scene(id: String, speaker: String, room: String, text: String, choices: Array) -> void:
	if fired.has(id):
		return
	fired[id] = true
	var ev := sim._new_radio("story", speaker, text, room, choices)
	ev["scene"] = id


## 이야기 장면의 선택 결과
func resolve(ev: Dictionary, key: String) -> Dictionary:
	var id: String = ev.get("scene", "")
	match id:
		"kim_retire":
			var k := _p("김 반장")
			if key == "promise":
				flags["kim_promise"] = true
				k.trust += 10.0
				return {"text": "김 반장: \"고맙습니다.\" 그는 조정실 벽의 달력에 동그라미를 쳤다.", "tone": "good"}
			return {"text": "김 반장: \"...그렇죠. 장담은 아무도 못 하죠.\"", "tone": "info"}
		"choi_rookie":
			var c := _p("최 반장")
			var im := _p("임 기사")
			if key == "back":
				c.trust += 15.0
				if im != null:
					im.add_stress(8.0, "공장장이 신참 반장 편을 들었다")
				return {"text": "최 반장의 목소리가 조금 커졌다. 임 기사는 대답 대신 담배를 꺼냈다.", "tone": "good"}
			c.trust -= 10.0
			return {"text": "최 반장: \"...알겠습니다.\" 무전이 짧게 끊겼다.", "tone": "info"}
		"ha_first":
			var ha := _p("하 사원")
			var kim := _p("김 반장")
			match key:
				"kim":
					flags["ha_supported"] = true
					ha.add_stress(-15.0, "사수가 옆에 있어 줬다")
					kim.trust += 5.0
					if kim.on_site:
						sim._send(kim, ha.room if ha.room != "" else "store", Callable())
					return {"text": "김 반장이 하 사원 옆에 의자를 끌어다 앉았다. \"세는 법 알려 줄게. 사람은 이름표부터 봐.\"", "tone": "good"}
				"patrol":
					flags["ha_supported"] = true
					ha.add_stress(-5.0, "순찰이 자주 들렀다")
					return {"text": "순찰이 30분마다 문을 두드리기로 했다.", "tone": "info"}
			ha.add_stress(12.0, "첫 야간을 혼자 버티라는 말을 들었다")
			return {"text": "하 사원: \"...네.\"", "tone": "info"}
		"jung_seal":
			var j := _p("정 사원")
			flags["seal_seen"] = true
			if key == "go":
				sim.records += 3
				j.add_contam(20.0, "봉인문 틈을 들여다봤다")
				return {"text": "정 사원이 돌아왔다. \"안에 불이 켜져 있어요. 계기판도 돌아가요. 누가... 일하고 있어요.\"\n관찰 기록 +3.", "tone": "bad"}
			j.add_stress(8.0, "봉인문에 못 가게 했다")
			return {"text": "정 사원: \"네...\" 그래도 그는 교대 내내 봉인문 쪽을 힐끔거렸다.", "tone": "info"}
		"double_stamp":
			if key == "fix":
				return {"text": "기록을 정정했다. 배 사원은 고개를 갸웃했다.", "tone": "info"}
			flags["double_kept"] = true
			return {"text": "기록은 그대로 남았다. 두 줄의 20:00.", "tone": "info"}
	return {"text": "", "tone": "none"}


## 이야기와 연결된 규칙 사건이 일어났을 때 (sim이 부른다)
func on_event(kind: String, crew: int) -> void:
	match kind:
		"pressure_done":
			find_memo("m2")
		"trace_done":
			find_memo("m3")
		"detected":
			find_memo("m4")
		"errand_done":
			find_memo("m5")
		"accident":
			if crew == 1 and flags.get("kim_promise", false) and not flags.has("kim_broken"):
				flags["kim_broken"] = true
				var k := _p("김 반장")
				if k != null:
					k.trust -= 20.0
					k.add_stress(30.0, "정년 전 무사고 약속이 깨졌다")
					sim._new_radio("story", "김 반장", "...약속하셨잖아요.", "control", [sim._choice("...", "ok")])


# ================================================================ 회의: 오늘 밤 걱정거리

## 다음 교대의 걱정거리. 플레이어가 미리 준비할 수 있는 것들.
func worries(day: int, shift: String, posts: Dictionary) -> Array:
	var out: Array = []
	var crew := Defs.crew_on(day, shift)
	# 이야기 예고
	if day == 3 and shift == "night":
		out.append("하 사원(신입)의 첫 야간이다. 혼자 있는 방에 배치되면 무서워할 것이다.")
	if day == 4 and shift == "night" and fired.has("ha_first"):
		out.append("하 사원의 두 번째 밤. 어젯밤 일이 오늘 어떻게 돌아올지.")
	if day == 2 and shift == "night":
		out.append("박 씨가 낮에 장 주임을 한참 쳐다봤다. 장 주임은 사흘째 잠을 못 잤다고 한다.")
	if day == 4 and shift == "night":
		out.append("정 사원이 저녁 먹으며 1호 반응기 봉인문 얘기를 했다.")
	# 사람
	for p in posts:
		var person: Person = p
		if person.fatigue >= 55:
			out.append("%s 피로 %d. 70이 넘으면 능력이 떨어진다." % [person.name, person.fatigue])
		if person.stress >= 60:
			out.append("%s 스트레스 %d. 100이면 그 교대에 무너진다. (원인: %s)" % [person.name, person.stress, person.top_reason()])
		if person.stage() >= 2:
			out.append("%s: 초점 없는 눈. 같은 방 사람이 불안해한다." % person.name)
	# 밤: 혼자 있는 방, 박 씨의 귀띔
	if shift == "night":
		var cnt := {}
		for p in posts:
			cnt[posts[p]] = cnt.get(posts[p], 0) + 1
		var alone: Array = []
		for id in ["reactor", "refine", "store"]:
			if cnt.get(id, 0) == 1:
				alone.append(Defs.ROOMS[id]["name"])
		if not alone.is_empty():
			out.append("혼자 일하는 방: %s. '한 명 더'가 생길 수 있는 조건이다." % ", ".join(alone))
		if day >= 2:
			var cand := ["store", "tank", "refine", "reactor", "dock"]
			park_hint_room = cand[sim.rng.randi() % cand.size()]
			park_hint_true = sim.rng.randf() < 0.7
			out.append("박 씨: \"오늘은 %s 불 켜 두세요. 그냥 그래야 할 것 같아서요.\"" % Defs.ROOMS[park_hint_room]["name"])
		else:
			park_hint_room = ""
	if sim.rate > 80:
		out.append("가동률 %d%%: 역압 위험이 있다%s." % [sim.rate, " (밤엔 1.5배)" if shift == "night" else ""])
	out.append(goal_line())
	return out


# ================================================================ 데모 엔딩

func ending_text() -> String:
	var lines: Array = []
	if sim.errand_person != null and sim.errand_state == "back":
		lines.append("10일차 아침, 출근 명단을 넘기다 손이 멈췄다.\n%s. 20:00. 그리고 한 줄 아래, 같은 이름, 같은 시각, 같은 필체." % sim.errand_person.name)
		lines.append("두 사람 다 지금 공장 안에 있다.")
	elif sim.errand_state == "refused":
		lines.append("10일차 아침, 박 씨가 경비실 창문을 두드렸다.\n\"공장장님, 그때 거절하셨죠. 괜찮아요. 그건 이미 스스로 골랐어요.\"")
		lines.append("출근 명단의 맨 아래에, 아무도 쓴 적 없는 이름이 하나 있다.")
	else:
		lines.append("10일차 아침. 출근 명단의 숫자가 하나 많다.")
	if sim.story.flags.get("kim_promise", false):
		if sim.story.flags.has("kim_broken"):
			lines.append("김 반장은 달력의 동그라미를 지웠다.")
		else:
			lines.append("김 반장은 달력에 동그라미를 하나 더 쳤다. \"남은 날도 이렇게만요.\"")
	lines.append("\n— 1막에서 계속 —")
	return "\n\n".join(lines)
