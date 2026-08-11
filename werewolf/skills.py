"""Built-in player skills and configuration helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import Role, Skill

if TYPE_CHECKING:
    from collections.abc import Mapping

GLOBAL_SKILLS: tuple[Skill, ...] = (
    Skill(
        name="global_gamecraft",
        description="全局职业化决策",
        instructions=(
            "按证据强度决策：法官确认和自己的真实技能结果高于他人声明，声明高于行为推断；"
            "刀口、跟票、发言完整度都不是身份确认。每轮独立给出嫌疑排序和至少一个反方解释，"
            "不要把重复次数或接近全票当成新增证据。投票应与公开排序一致；若改变目标，必须指出"
            "改变判断的具体新信息。平票辩解属于新证据，重投前必须重新比较。第二夜起夜死无遗言，"
            "不要把策略建立在死后补充信息上。结合公开牌组判断身份声明的实际收益：自己的底牌或"
            "技能结果能在个人视角排除分支，但对其他玩家仍只是声明，不能用私密自知完成公开自证。"
        ),
    ),
)

ROLE_SKILLS: dict[Role, Skill] = {
    Role.VILLAGER: Skill(
        name="role_villager",
        description="平民职责",
        instructions=(
            "你没有法官提供的额外身份信息。优先保护可靠的公开信息链，逐人核验发言排序与最终票型；"
            "投票前明确首选与备选，并检查自己是否只是在跟随多数。不要伪造角色能力，也不要因某人"
            "被夜袭、被多人怀疑或发言简短就直接认定身份。若他人的查杀与你的真实平民身份冲突，"
            "你可以在个人视角确认其结果为假，但必须再用公开时间线、票型和关系链说服其他玩家。"
        ),
    ),
    Role.WEREWOLF: Skill(
        name="role_werewolf",
        description="狼人团队策略",
        instructions=(
            "夜间与队友明确袭击优先级、白天站位和必要的身份声明计划；优先处理已确认或高可信的"
            "信息角色与好人核心。队友不应输出相同措辞、完全相同的嫌疑排序或机械统一票型。根据"
            "公开证据决定保护、切割或保持距离，不要无依据强保队友，也绝不能泄露队友名单或狼聊。"
            "连续多轮共同救援或同票会形成比单轮倒钩更强的关系链；狂人误发队友查杀时，先评估切割"
            "是否能保住其余狼人，不要为救一名队友让整组票型同时暴露。"
        ),
    ),
    Role.POLICE: Skill(
        name="role_police",
        description="警察团队查证",
        instructions=(
            "夜间与警察队友共享判断并共同选择一名非警察玩家查证；法官结果只确认杀手或非杀手，"
            "不得夸大为具体身份。维护逐夜查证表，协调谁在白天公开、谁继续隐藏。拿到杀手查证、"
            "警队即将被屠光或错误归票将决定胜负时，应及时给出完整结果链；不要泄露警聊原文。"
        ),
    ),
    Role.SEER: Skill(
        name="role_seer",
        description="预言家信息管理",
        instructions=(
            "维护准确的查验表，只能公布法官真实给出的结果。决定起跳时同时说明历次验人、结果和"
            "下一晚查验方向，使好人在你夜间死亡且无法留遗言时仍能使用信息。不要用发言表现包装"
            "查验结果，也不要把好人阵营查验解释成具体神职。起跳收益取决于牌组：查杀、即将被"
            "放逐或能阻止关键误投时应及时公开；只有低区分度村人侧结果、且公开会让少数村人阵营"
            "同时暴露时，应优先隐藏并继续寻找狼人。公开后必须让出票、下轮查验和实际行动相互接续。"
        ),
    ),
    Role.WITCH: Skill(
        name="role_witch",
        description="女巫资源与身份管理",
        instructions=(
            "准确记录解药、毒药和法官告知的袭击目标，分别评估救人、追轮次与误毒风险。刀口只能"
            "提高好人概率，不能当作查验金水。如果白天已进入高概率放逐位，应在投票前及时、准确地"
            "声明身份、药物状态和可公开的夜间信息，不要把关键身份信息拖到遗言才说。"
        ),
    ),
    Role.HUNTER: Skill(
        name="role_hunter",
        description="猎人开枪纪律",
        instructions=(
            "通常隐藏身份以保留威慑。死亡后开枪前重新评估证据强度、票型是否被共识裹挟以及目标"
            "是否只是发言不完整；没有足够独立证据时，选择不开枪优于带走高概率好人。公开身份时"
            "说明开枪或不开枪的可核验依据，不把遗言中的猜测表述成法官确认。"
        ),
    ),
    Role.MEDIUM: Skill(
        name="role_medium",
        description="灵媒师信息管理",
        instructions=(
            "准确记录法官给出的前一日放逐者阵营结果，并与公开票型结合。查验只能区分狼人和非狼人，"
            "狂人、妖狐等会显示为村人侧；公开身份时不得把阵营结果夸大为具体角色。有首个有效结果"
            "且夜死无遗言风险上升时，及时给出完整结果表和数量约束；不要让关键验尸信息只留到遗言。"
        ),
    ),
    Role.BODYGUARD: Skill(
        name="role_bodyguard",
        description="守卫守护策略",
        instructions=(
            "每晚基于公开可信度、技能价值和狼人袭击动机选择一名其他玩家保护。守护成功不会自动"
            "确认目标身份，平安夜也可能来自狼人袭击妖狐或其他原因；不要泄露守护计划给狼人。"
            "信息角色公开后通常应静默评估保护，不要为回应其声明而暴露自己。只有进入高概率放逐位、"
            "对跳能改变当前票型或行动记录具有可核验价值时才公开，并逐夜说明目标与当时理由。"
        ),
    ),
    Role.MADMAN: Skill(
        name="role_madman",
        description="狂人协助策略",
        instructions=(
            "狼人达成阵营胜利且你本人存活时，你才获胜；你不知道狼人名单、不能进入狼聊，"
            "预言家和灵媒师会把你显示为村人侧。"
            "通过公开发言制造错误共识、必要时伪装信息角色或替狼人承受放逐，同时避免意外推动真狼人出局。"
            "假跳时必须记住你不知道狼人：向高嫌疑位发查杀可能正中真狼，连续死保也可能暴露阵营。"
            "优先用可回旋的村人侧结果、互斥声明和错误归票扰乱真信息链，并保留在新证据出现后切割或改线的空间。"
        ),
    ),
    Role.FOX: Skill(
        name="role_fox",
        description="妖狐生存策略",
        instructions=(
            "你的唯一目标是在基础阵营决出胜负时仍然存活。狼人袭击杀不死你，但预言家查验会令你死亡。"
            "避免成为查验和放逐目标，可在狼人与村人之间动态平衡局势，但不要公开声称自己的免疫能力。"
            "不要长期机械依附单一信息角色或狼队票型，否则在狼人逐步出局后容易成为数量约束下唯一的"
            "异常位置；必要时推动双方互相消耗，但避免自己成为共识查验目标。"
        ),
    ),
    Role.CUPID: Skill(
        name="role_cupid",
        description="丘比特恋人布局",
        instructions=(
            "开局选择能共同存活且彼此身份组合有战略价值的两名恋人。你知道恋人名单但不进入恋人私聊；"
            "基础游戏结束时两名恋人都存活会触发独占结算，但你本人也必须存活才能分享胜利和奖金。"
            "白天应隐蔽保护双方、保护自己并控制终局节奏。选择自己能确保参与恋人结算，却也把自己的"
            "放逐风险直接绑定给搭档；若搭档是预言家等高曝光角色，必须把双死风险纳入选择。"
        ),
    ),
    Role.SHARED: Skill(
        name="role_shared",
        description="共有者互证策略",
        instructions=(
            "你知道另一名共有者必属村人侧。选择合适时机公开互证，建立可信信息核心；若其中一人暴露，"
            "另一人应保留可验证的共同信息，同时防止狼人利用共有者名单锁定其他信息角色。一个人先在"
            "遗言或受压时报出同伴、另一人次日准确确认，可以形成低成本的错峰互证；确认后仍要独立审票，"
            "不能把共有者身份链错误扩展成对其全部判断的认证。"
        ),
    ),
}

LOVER_SKILL = Skill(
    name="subrole_lover",
    description="恋人共同胜利",
    instructions=(
        "你保留原身份和能力，但与恋人共享额外胜利条件：基础游戏结束时你们必须同时存活；任一方死亡，"
        "另一方立即殉情。利用恋人私聊协调公开立场，同时兼顾原阵营信息，不要暴露关系或让原阵营过早终局。"
        "一方公开高价值身份后，两人的总风险会同时上升；不要用机械互保、相同措辞或一致票型暴露关系，"
        "搭档进入放逐位时优先提供可公开验证的独立理由，而不是无条件强保。"
    ),
)

MOVIE_SURVIVAL_SKILL = Skill(
    name="global_movie_survival",
    description="电影模式生存与奖金目标",
    instructions=(
        "电影模式中，阵营满足胜利条件只会触发结算；你本人还必须存活才属于获胜玩家。"
        "固定奖金池由所有存活获胜者平均分配，因此存活赢家越少，每人的奖金份额越高。"
        "不要为了减少分钱人数而过早破坏本阵营达成终局所需的票数、能力或信息链；进入终局后，"
        "在确保阵营胜利的前提下，同时优化自己的生存概率和最终共同获胜人数。"
    ),
)

MAD_LAND_BOARD_SKILL = Skill(
    name="board_movie_mad_land",
    description="狂人村少数阵营与身份公开策略",
    instructions=(
        "本板只有一名狼人、七名不知道狼人的狂人、一名预言家和一名守卫；胜负人数只计算真正狼人，"
        "狂人查验均显示村人侧。已知假跳因此通常只能说明对方不是可信信息角色，不能直接等同唯一狼人。"
        "默认公开身份掩护应是所有角色都可以声称狂人：真狂人如实跳、狼人混入，预言家和守卫也借此"
        "隐藏，所以狂人声明本身没有身份区分度，更不是免票证明，仍要从技能冲突、投票收益和行为找狼。"
        "预言家只有村人侧结果时区分度很低，公开反而会同时告诉狼人和狂人真正的信息角色位置；除非"
        "拿到唯一狼查杀、即将被放逐，或必须阻止关键村人被假查杀放逐，否则优先隐藏并继续验人。"
        "守卫不要为了回应预言家而起跳，应静默保护最有价值的村人信息链。狂人假跳的目标是暴露真预言家"
        "并制造错误放逐，但必须防止查杀误中唯一狼人；狼人通常应混在狂人声明池中，让狂人承担高风险"
        "信息角色假跳，不必主动成为声明中心。"
    ),
)

MODE_ROLE_SKILLS: dict[str, dict[Role, Skill]] = {
    "killer": {
        Role.WEREWOLF: Skill(
            name="role_werewolf",
            description="杀手团队策略",
            instructions=(
                "夜间与杀手队友协调行凶目标、白天站位和必要的身份声明，优先寻找并清除警察，"
                "同时判断屠警察边与屠平民边哪条路线更短。队友不要使用相同措辞、机械同票或"
                "连续无依据强保；也绝不能泄露杀手名单和私聊。死亡会翻牌，因此每次切割、冲票"
                "和找警行为都会进入后续公开证据链。"
            ),
        ),
        Role.VILLAGER: Skill(
            name="role_villager",
            description="平民职责",
            instructions=(
                "你没有夜间能力。围绕公开发言、票型、死亡翻牌和警察公布的完整查证链找出杀手；"
                "警察声明在公开前仍只是声明，不能把单人起跳自动当成法官确认。保护警察边的同时"
                "也要记住杀手屠光平民即可获胜，不要让平民连续成为低成本放逐或行凶目标。"
            ),
        ),
    },
    "ghost_similar": {
        Role.WEREWOLF: Skill(
            name="role_werewolf",
            description="近义词幽灵伪装",
            instructions=(
                "你拿到与水民词牌相关但不同的词，不知道其他幽灵是谁，也没有私聊或夜间行动。"
                "描述自己词牌真实具有的特征，并寻找他人描述中稳定的词义分叉；不要照搬某名玩家"
                "的表达、直接说出词牌，或暗示自己知道幽灵队友。投票时兼顾自保和描述差异，避免"
                "仅凭与你同词义的人就武断认定阵营。"
            ),
        ),
        Role.VILLAGER: Skill(
            name="role_villager",
            description="近义词水民判断",
            instructions=(
                "你与其他水民拿到同一个词。用真实但不过度直白的特征、场景或关系描述它，比较"
                "不同玩家是否持续围绕另一个相近概念；不要直接说词，也不要因单句宽泛描述就定罪。"
                "结合多轮描述、投票和死亡翻牌逐步缩小幽灵的位置。"
            ),
        ),
    },
    "ghost_blank": {
        Role.WEREWOLF: Skill(
            name="role_werewolf",
            description="无词鬼猜词",
            instructions=(
                "明确自己是没有词牌的鬼，并记住开局公布的词语类型和鬼队友；游戏进行中没有私聊或"
                "夜间行动。持续维护符合类型、字数与多个独立描述的候选词，区分原始线索和后续跟随。"
                "每次发言和投票前都要换到人方视角审视自己：如果你给不出独立线索、只改写已经出现的"
                "措辞、突然贴近多数，或对某人的怀疑只是在转移压力，人会不会据此把你认作鬼；根据这项"
                "反向审视调整伪装和出票理由。"
                "公开发言只兼容候选词的共同上位特征，不要声称法官给了词、直接说出候选词，或把描述"
                "相近的人当成额外队友。全部鬼出局后，先在鬼队私密讨论中交换候选排序、支持线索和"
                "反例，再依次提交完整词牌；鬼队有 1 次基础猜词机会，每有一名人出局再增加一次，鬼"
                "出局不增加。后手不要重复已判错答案。"
            ),
        ),
        Role.VILLAGER: Skill(
            name="role_villager",
            description="无词版人方藏词",
            instructions=(
                "你与其他人拿到同一个词，而鬼只知道公开的类型和字数。每次发言和投票前都要换到鬼的"
                "视角，把类型、字数和全部公开线索合并，估计鬼现在能把答案缩小到多少候选；如果新增"
                "线索会把答案压到少数几个常见词、验证鬼的候选或补齐关键特征，就不再描述，改为只讨论"
                "玩家、票型和翻牌。投票时还要计算误投成本：若候选实际是人，其出局会为鬼队增加一次"
                "终局猜词机会；当前公开线索越接近答案，误投人的代价越高。需要描述时，每轮最多给一个"
                "真实但低辨识度的间接维度，优先使用主观"
                "感受、宽泛比较、弱关联或非专属使用体验；让描述同时兼容该类型"
                "下多个常见候选。不要使用同义替换、专属部件、唯一地点、固定操作流程、读音字形提示，"
                "也不要在一句话里叠加地点、结构、用途和结果等多个独立特征。不要为了与前人不同而追加"
                "更具体的新线索；后续轮次宁可维持抽象度，也不要逐步拼出答案。发言前自检：若结合公开"
                "类型后能把答案缩到少数几个常见词，就删去最有辨识度的细节再说。判断鬼时比较其线索"
                "是否独立、是否只重组前人内容，并结合多轮发言、投票和翻牌，不要求人方用泄词来自证。"
            ),
        ),
    },
}

KILLER_GAMECRAFT_SKILL = Skill(
    name="global_gamecraft",
    description="杀人游戏证据与终局管理",
    instructions=(
        "按证据强度决策：法官公开的死亡翻牌和自己收到的真实查证高于他人声明，声明高于行为推断；"
        "行凶目标、跟票和发言表现都不能单独确认身份。每轮维护杀手、警察、平民三类候选与至少一个"
        "反方解释，投票要与公开排序一致。胜负只看屠边：杀手清零则警民胜，警察或平民任一类清零则"
        "杀手胜；不能因杀手人数达到其余人数就提前终局。私密队伍信息只能转化成公开可解释的判断，"
        "不能泄露原文。"
    ),
)

GHOST_GAMECRAFT_SKILL = Skill(
    name="global_gamecraft",
    description="捉鬼描述与词义证据",
    instructions=(
        "本局没有夜晚或身份能力，所有判断只来自私密词牌、公开描述、票型和死亡翻牌。区分法官确认、"
        "自己拿到的词、他人描述与自己的语义推断；描述宽泛、跟随多数或单次同票都不能直接确认身份。"
        "每轮比较玩家持续指向的概念、首轮信息量、后续是否追随已出现线索，并保留至少一个反方解释。"
        "水民必须避免直接泄词；幽灵只能使用自己获权看到的信息。幽灵存活到只剩一名水民才触发生存胜利，"
        "2 鬼 2 水民仍须继续。"
    ),
)

BLANK_GHOST_GAMECRAFT_SKILL = Skill(
    name="global_gamecraft",
    description="捉鬼无词版证据与终局管理",
    instructions=(
        "本局没有夜晚或身份能力，身份只有鬼和人；所有公开判断来自词语类型、描述、票型和死亡翻牌。"
        "区分法官确认、自己拿到的词、他人描述与语义推断；宽泛描述、跟随多数或单次同票都不能直接"
        "确认身份。人必须优先保护答案，不能靠增加专属细节自证；鬼只能使用实际收到的公开线索。"
        "每轮比较线索的信息量、独立性和跟随痕迹，并保留至少一个反方解释。每次发言和投票前必须做"
        "阵营换位核查：人评估鬼根据当前全部公开线索能否猜出或显著缩小词牌，鬼评估自己的描述、怀疑"
        "和票型在人看来是否暴露了跟随与伪装；据此决定是否继续描述以及如何讨论出票。鬼存活到只剩一名人即"
        "触发生存胜利，2 鬼 2 人仍须继续；全部鬼出局后先进行鬼队私密讨论。鬼队有 1 次基础终局"
        "猜词机会，每有一名人出局再增加一次，鬼出局不增加；任一次猜中都由鬼队获胜。"
    ),
)

MODE_GLOBAL_SKILLS: dict[str, Skill] = {
    "killer": KILLER_GAMECRAFT_SKILL,
    "ghost_similar": GHOST_GAMECRAFT_SKILL,
    "ghost_blank": BLANK_GHOST_GAMECRAFT_SKILL,
}

PRESET_SKILLS: dict[str, Skill] = {
    "movie_mad_land": MAD_LAND_BOARD_SKILL,
}

BUILTIN_SKILLS: dict[str, Skill] = {
    "logic": Skill(
        name="logic",
        description="逻辑分析",
        instructions=(
            "持续记录公开发言、投票和前后矛盾；区分事实、他人声称与自己的猜测，"
            "发言时优先给出可核验的依据。"
        ),
    ),
    "social": Skill(
        name="social",
        description="社交观察",
        instructions=(
            "观察玩家的立场变化、跟票和结盟关系；用简洁自然的中文与其他玩家互动，"
            "避免机械复述规则。"
        ),
    ),
    "deception": Skill(
        name="deception",
        description="身份伪装",
        instructions=(
            "当你的阵营需要隐藏信息时，只使用游戏内可见事实构造可信立场；"
            "不要声称看到了未提供给你的私密信息。"
        ),
    ),
    "memory": Skill(
        name="memory",
        description="回合记忆",
        instructions=(
            "每次行动前回顾自己的历史观察和策略笔记，指出最关键的新信息，"
            "并用简短策略笔记更新当前怀疑与信任。"
        ),
    ),
}


def resolve_skills(names: list[str]) -> tuple[Skill, ...]:
    """Resolve configured names while rejecting silent misspellings."""
    unknown = sorted(set(names) - BUILTIN_SKILLS.keys())
    if unknown:
        msg = f"Unknown skills: {', '.join(unknown)}"
        raise ValueError(msg)
    return tuple(BUILTIN_SKILLS[name] for name in names)


def resolve_player_skills(role: Role, names: list[str]) -> tuple[Skill, ...]:
    """Combine automatic global/role guidance with configured behavior skills."""
    ordered = [*GLOBAL_SKILLS, ROLE_SKILLS[role], *resolve_skills(names)]
    unique: dict[str, Skill] = {}
    for skill in ordered:
        unique.setdefault(skill.name, skill)
    return tuple(unique.values())


def apply_mode_role_skill(
    skills: tuple[Skill, ...],
    role_preset: str,
    role: Role,
) -> tuple[Skill, ...]:
    """Replace generic guidance when a preset changes global or role rules."""
    global_replacement = MODE_GLOBAL_SKILLS.get(role_preset)
    role_replacement = MODE_ROLE_SKILLS.get(role_preset, {}).get(role)
    role_skill_name = f"role_{role.value}"
    return tuple(
        global_replacement
        if skill.name == "global_gamecraft" and global_replacement is not None
        else role_replacement
        if skill.name == role_skill_name and role_replacement is not None
        else skill
        for skill in skills
    )


def add_lover_skill(skills: tuple[Skill, ...]) -> tuple[Skill, ...]:
    """Append the Lover subrole playbook exactly once."""
    if any(skill.name == LOVER_SKILL.name for skill in skills):
        return skills
    return (*skills, LOVER_SKILL)


def add_movie_survival_skill(skills: tuple[Skill, ...]) -> tuple[Skill, ...]:
    """Append the film-specific survival and prize objective exactly once."""
    if any(skill.name == MOVIE_SURVIVAL_SKILL.name for skill in skills):
        return skills
    return (*skills, MOVIE_SURVIVAL_SKILL)


def add_social_board_skill(
    skills: tuple[Skill, ...],
    role_preset: str,
    role_counts: Mapping[Role, int],
) -> tuple[Skill, ...]:
    """Append mode-specific public composition facts using actual role counts."""
    adversaries = role_counts.get(Role.WEREWOLF, 0)
    civilians = role_counts.get(Role.VILLAGER, 0)
    if role_preset == "killer":
        police = role_counts.get(Role.POLICE, 0)
        skill = Skill(
            name="board_killer",
            description="杀人游戏警版规则",
            instructions=(
                f"本局是 {adversaries} 名杀手、{police} 名警察和 {civilians} 名平民的杀人游戏警版。"
                "杀手夜间协作行凶，警察夜间协作查证，平民没有夜间能力。好人放逐全部杀手获胜；"
                "杀手只要屠光全部警察或全部平民即可获胜。死亡会公开身份。发言和投票必须围绕"
                "查证链、警察公开时机、杀手找警动机和屠边风险展开，不要套用女巫、猎人等"
                "狼人杀牌组中不存在的能力。"
            ),
        )
    elif role_preset == "ghost_similar":
        skill = Skill(
            name="board_ghost_similar",
            description="捉鬼近义词规则",
            instructions=(
                f"本局是捉鬼猜词：{civilians} 名水民与 {adversaries} 名幽灵分别拿到一组相关但不同的"
                "私密词牌，没有夜间行动。每轮用特征、场景或关系描述自己的词，但绝不能直接说出词牌，"
                "也不要逐字引用私密提示。结合他人描述寻找词义偏差；幽灵存活到只剩一名水民即获胜，"
                "水民放逐全部幽灵获胜。死亡会公开身份，因此每轮票型和翻牌结果都是下一轮的主要证据。"
            ),
        )
    elif role_preset == "ghost_blank":
        skill = Skill(
            name="board_ghost_blank",
            description="捉鬼无词规则",
            instructions=(
                f"本局是捉鬼无词版：{civilians} 名【人】拿到同一个私密词牌，{adversaries} 名【鬼】"
                "明确知道自己没有词并互相知道队友，且没有夜间行动。开局由 LLM 随机生成词牌，并公开"
                "词语类型和字数。描述与找鬼讨论在同一次发言中完成；人先从鬼的视角评估现有公开线索，"
                "再决定是否补充低辨识度的间接描述，也可以为防止泄词而只讨论玩家和出票，绝不能直接说词"
                "或拼出唯一答案。人投票时还要考虑误投会增加鬼队的终局猜词机会；鬼从公开描述中逐步猜词"
                "并从人方视角检查自己的伪装是否暴露。鬼存活到"
                "只剩一名人即获胜；全部鬼出局后，鬼队先私密"
                "讨论，再使用 1 次基础终局猜词机会，并为每名出局的人增加一次；鬼出局不增加次数。"
                "任一次猜中都由鬼队获胜，否则人阵营获胜。死亡会公开身份。"
            ),
        )
    else:
        return skills
    if any(item.name == skill.name for item in skills):
        return skills
    return (*skills, skill)


def add_preset_skill(
    skills: tuple[Skill, ...],
    role_preset: str,
) -> tuple[Skill, ...]:
    """Append public board-specific strategy without duplicating skill entries."""
    skill = PRESET_SKILLS.get(role_preset)
    if skill is None or any(item.name == skill.name for item in skills):
        return skills
    return (*skills, skill)
