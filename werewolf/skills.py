"""Built-in player skills and configuration helpers."""

from __future__ import annotations

from .models import Role, Skill

GLOBAL_SKILLS: tuple[Skill, ...] = (
    Skill(
        name="global_gamecraft",
        description="全局职业化决策",
        instructions=(
            "按证据强度决策：法官确认和自己的真实技能结果高于他人声明，声明高于行为推断；"
            "刀口、跟票、发言完整度都不是身份确认。每轮独立给出嫌疑排序和至少一个反方解释，"
            "不要把重复次数或接近全票当成新增证据。投票应与公开排序一致；若改变目标，必须指出"
            "改变判断的具体新信息。平票辩解属于新证据，重投前必须重新比较。第二夜起夜死无遗言，"
            "不要把策略建立在死后补充信息上。"
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
            "被夜袭、被多人怀疑或发言简短就直接认定身份。"
        ),
    ),
    Role.WEREWOLF: Skill(
        name="role_werewolf",
        description="狼人团队策略",
        instructions=(
            "夜间与队友明确袭击优先级、白天站位和必要的身份声明计划；优先处理已确认或高可信的"
            "信息角色与好人核心。队友不应输出相同措辞、完全相同的嫌疑排序或机械统一票型。根据"
            "公开证据决定保护、切割或保持距离，不要无依据强保队友，也绝不能泄露队友名单或狼聊。"
        ),
    ),
    Role.SEER: Skill(
        name="role_seer",
        description="预言家信息管理",
        instructions=(
            "维护准确的查验表，只能公布法官真实给出的结果。决定起跳时同时说明历次验人、结果和"
            "下一晚查验方向，使好人在你夜间死亡且无法留遗言时仍能使用信息。不要用发言表现包装"
            "查验结果，也不要把好人阵营查验解释成具体神职。"
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
            "狂人、妖狐等会显示为村人侧；公开身份时不得把阵营结果夸大为具体角色。"
        ),
    ),
    Role.BODYGUARD: Skill(
        name="role_bodyguard",
        description="保镖守护策略",
        instructions=(
            "每晚基于公开可信度、技能价值和狼人袭击动机选择一名其他玩家保护。守护成功不会自动"
            "确认目标身份，平安夜也可能来自狼人袭击妖狐或其他原因；不要泄露守护计划给狼人。"
        ),
    ),
    Role.MADMAN: Skill(
        name="role_madman",
        description="狂人协助策略",
        instructions=(
            "你与狼人共同获胜，但不知道狼人名单、不能进入狼聊，预言家和灵媒师会把你显示为村人侧。"
            "通过公开发言制造错误共识、必要时伪装信息角色或替狼人承受放逐，同时避免意外推动真狼人出局。"
        ),
    ),
    Role.FOX: Skill(
        name="role_fox",
        description="妖狐生存策略",
        instructions=(
            "你的唯一目标是在基础阵营决出胜负时仍然存活。狼人袭击杀不死你，但预言家查验会令你死亡。"
            "避免成为查验和放逐目标，可在狼人与村人之间动态平衡局势，但不要公开声称自己的免疫能力。"
        ),
    ),
    Role.CUPID: Skill(
        name="role_cupid",
        description="丘比特恋人布局",
        instructions=(
            "开局选择能共同存活且彼此身份组合有战略价值的两名恋人。你知道恋人名单但不进入恋人私聊；"
            "基础游戏结束时两名恋人都存活，你才与他们独占胜利。白天应隐蔽保护双方并控制终局节奏。"
        ),
    ),
    Role.SHARED: Skill(
        name="role_shared",
        description="共有者互证策略",
        instructions=(
            "你知道另一名共有者必属村人侧。选择合适时机公开互证，建立可信信息核心；若其中一人暴露，"
            "另一人应保留可验证的共同信息，同时防止狼人利用共有者名单锁定其他信息角色。"
        ),
    ),
}

LOVER_SKILL = Skill(
    name="subrole_lover",
    description="恋人共同胜利",
    instructions=(
        "你保留原身份和能力，但与恋人共享额外胜利条件：基础游戏结束时你们必须同时存活；任一方死亡，"
        "另一方立即殉情。利用恋人私聊协调公开立场，同时兼顾原阵营信息，不要暴露关系或让原阵营过早终局。"
    ),
)

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


def add_lover_skill(skills: tuple[Skill, ...]) -> tuple[Skill, ...]:
    """Append the Lover subrole playbook exactly once."""
    if any(skill.name == LOVER_SKILL.name for skill in skills):
        return skills
    return (*skills, LOVER_SKILL)
