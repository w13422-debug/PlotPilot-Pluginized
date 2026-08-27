"""Read-only parity adapter for the v4.6.0 language-style patterns."""
from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    severity: str
    message: str
    start: int
    end: int
    excerpt: str
    source_hash: str


EIGHT_LEGS = ((r"首先.{0,10}其次.{0,10}最后", "八股文三段式"), (r"第一.{0,10}第二.{0,10}第三", "八股文三段式"), (r"先是.{0,10}随后.{0,10}(最终|最后|终于)", "八股文三段式(先是/随后/最终)"), (r"先是.{0,8}接着.{0,8}(然后|最后)", "八股文三段式(先是/接着/然后)"), (r"起初.{0,10}后来.{0,10}(最终|最后|终于)", "八股文三段式(起初/后来/最终)"), (r"一方面.{0,10}另一方面", "八股文两面式"), (r"不仅.*?而且.*?更", "八股文递进式"), (r"总体而言|综上所述|总而言之", "八股文总结式"), (r"从.{1,8}角度(来看|来说)", "八股文分析式"), (r"这体现了|这反映了|这表明了", "八股文解读式"))
SIMILES = ((r"像.{1,6}一样.{2,8}", "明喻（像X一样Y）"), (r"仿佛.{1,6}一般.{2,8}", "明喻（仿佛X一般Y）"), (r"犹如.{1,6}般", "明喻（犹如X般）"), (r"如同.{1,4}似.{1,4}", "明喻（如同X似Y）"))
OVER_RATIONAL = ((r"(分析|评估|判断|权衡|考量).{0,6}(自己的|对方的|当前的)", "情绪场景中的理性分析"), (r"(意识到|认识到|体会到).{0,10}(重要性|关键性|必要性)", "感悟式理性总结"), (r"(从.*?的角度|站在.*?立场).{0,10}(思考|审视|看待)", "视角式理性分析"), (r"(内心深处|心底).{0,10}(明白|清楚|知道).{0,10}(必须|应该|需要)", "内心独白式说教"), (r"(评估|分析|计算|衡量).{0,10}(投入|回报|成本|收益|风险)", "商业分析式理性"), (r"(开始|试图|试着).{0,6}(分析|评估|判断|权衡|考量).{0,15}(关系|感情|情感|局面)", "情绪场景中突然理性分析"))
DETOURS = ((r"不由自主地.{2,10}了一种.{2,10}的.{2,10}感觉", "感受拐弯描写"), (r"仿佛.{2,8}般地.{2,8}着.{2,8}的.{2,8}", "多重修饰拐弯"), (r"一种无法(言说|形容|描述|言喻).{0,10}的.{2,10}", "无法言说式拐弯"), (r"在.{2,6}(之中|之间|之内).{2,10}的.{2,10}", "嵌套结构拐弯"), (r"一种.{2,8}的.{2,8}的.{2,8}(感觉|感受|情绪|冲动)", "多重定语拐弯"), (r"(从|自).{2,8}(涌上|升起|产生|袭来).{0,8}(的|着).{0,8}(感觉|感受|冲动)", "来源式拐弯描写"))


def scan_language_style(content: str) -> tuple[Finding, ...]:
    digest = sha256(content.encode("utf-8")).hexdigest()
    found: list[Finding] = []

    def add(rule: str, severity: str, patterns: tuple[tuple[str, str], ...], *, skip_first: bool = False) -> None:
        count = 0
        for pattern, description in patterns:
            for match in re.finditer(pattern, content):
                count += 1
                if skip_first and count == 1:
                    continue
                found.append(Finding(rule, severity, description, match.start(), match.end(), match.group(0), digest))

    add("style.eight_legs", "warning", EIGHT_LEGS)
    add("style.number_metaphor", "info", SIMILES, skip_first=True)
    add("style.over_rational", "warning", OVER_RATIONAL)
    add("style.detour", "warning", DETOURS)
    return tuple(sorted(found, key=lambda item: (item.start, item.rule_id)))
