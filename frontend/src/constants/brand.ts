export const BRAND = {
  productName: 'PlotPilot',
  chineseName: '墨枢',
  displayName: 'PlotPilot · 墨枢（插件化改造版）',
  tagline: '基于既有源码的插件化小说工作台',
  descriptor: 'PlotPilot 插件化改造项目',
  team: '独立插件化改造项目',
  credit: '基于既有 PlotPilot 源码的插件化改造与 WebUI 集成（非原作者官方版本）',
  groupLabel: 'QQ群：663844122',
} as const

export const BRAND_COPY = {
  short: BRAND.displayName,
  compact: `${BRAND.chineseName} · ${BRAND.tagline}`,
  full: `${BRAND.displayName}｜${BRAND.credit}`,
  social: BRAND.groupLabel,
} as const
