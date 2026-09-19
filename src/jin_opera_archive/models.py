"""档案领域共用常量与小工具。"""

from __future__ import annotations

# 影像传播用途：研究、展览、网络传播、出版
PURPOSES = ("research", "exhibition", "web", "publication")
PURPOSE_LABELS = {
    "research": "研究",
    "exhibition": "展览",
    "web": "网络传播",
    "publication": "出版",
}

# 权利主体类型
PERSON = "person"
ORGANIZATION = "organization"

# 院团类型
TROUPE = "troupe"
PUBLISHER = "publisher"
OTHER_ORG = "other"

# 身份确认状态
IDENTITY_CONFIRMED = "confirmed"
IDENTITY_PROVISIONAL = "provisional"
IDENTITY_MERGED = "merged"

# 候选关系状态
CANDIDATE_PENDING = "pending"
CANDIDATE_CONFIRMED = "confirmed"
CANDIDATE_REJECTED = "rejected"

# 授权状态
LICENSE_ACTIVE = "active"
LICENSE_WITHDRAWN = "withdrawn"

CERTAINTY = ("confirmed", "provisional")


def normalize_name(name: str) -> str:
    """归一化名称用于比对：去空白与常见标点，不做小写（中文无大小写）。"""
    return "".join(ch for ch in (name or "") if ch not in " 　·・,，.。:：;；()（）[]【】\"'")
