---
version: alpha
name: Bridge Admin Dashboard
description: "GitHub-Dark inspired operational dashboard for the LanMeng-JKY bridge service. Dark canvas (#0d1117), blue accent (#58a6ff) for interactive elements, green (#3fb950) for success, red (#f85149) for errors, amber (#d29922) for warnings. Monospace green (#7ee787) for code/technical data. The aesthetic is utilitarian-first: dense tables, minimal chrome, semantic color carries meaning. No decorative elements, no animations — every pixel serves ops-monitoring efficiency."
author: Hermes Agent (2026-07-03)
based_on: "GitHub Dark color primitives + Linear DESIGN.md structure"

colors:
  # Background
  canvas: "#0d1117"         # Page background, modal overlay
  surface-1: "#161b22"      # Cards, tables header, modal content, panel body
  surface-2: "#21262d"      # Tab bar (inactive), filter inputs, pagination buttons, select
  surface-3: "#1c2128"      # Row hover
  surface-4: "#1a2332"      # Selected row (checkbox)

  # Borders
  hairline: "#30363d"       # Table rows, card borders, tab borders, input borders, modal borders

  # Text
  ink: "#c9d1d9"            # Body text, table content
  ink-muted: "#8b949e"      # Description text, stat labels, empty states, timestamps, secondary labels
  ink-dim: "#f0f6fc"        # Headers (h2), active tab label
  ink-heading: "#58a6ff"    # h1 title
  ink-tab-active: "#f0f6fc" # Active tab text
  ink-tab-inactive: "#8b949e" # Inactive tab text

  # Accent - Blue (primary interaction)
  primary: "#58a6ff"        # h1 title, stat numbers, expand button, accent-color
  primary-hover: "#79c0ff"  # (button hover — not used yet but reserved)

  # Accent - Checkbox
  checkbox-accent: "#238636" # Checkbox fill, action bar button background

  # Badge
  badge-ok-bg: "#1b4332"    # Success badge background
  badge-ok-text: "#3fb950"  # Success badge text
  badge-err-bg: "#4d1a1a"   # Error badge background
  badge-err-text: "#f85149" # Error badge text
  badge-warn-bg: "#4d351a"  # Warning badge background
  badge-warn-text: "#d29922" # Warning badge text
  badge-init-bg: "#1a3a4d"  # Info/init badge background
  badge-init-text: "#58a6ff" # Info/init badge text

  # Code / Data
  code-text: "#7ee787"      # Monospace code color (table data, JSON)
  code-bg: "#0d1117"        # Code block background (modal content)

  # Result messages
  result-ok-bg: "#1b4332"
  result-ok-border: "#3fb950"
  result-ok-text: "#3fb950"
  result-err-bg: "#4d1a1a"
  result-err-border: "#f85149"
  result-err-text: "#f85149"

typography:
  body:
    fontFamily: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif
    fontSize: 12px
    fontWeight: 400
    color: "{colors.ink}"
  heading-1:
    fontSize: 22px
    fontWeight: 600
    color: "{colors.ink-heading}"
  heading-2:
    fontSize: 16px
    fontWeight: 600
    color: "{colors.ink-dim}"
    borderBottom: "1px solid {colors.hairline}"
  description:
    fontSize: 13px
    color: "{colors.ink-muted}"
  table-header:
    fontSize: 12px
    fontWeight: 600
    color: "{colors.ink-muted}"
  table-data:
    fontSize: 12px
    color: "{colors.ink}"
  table-data-code:
    fontFamily: "'SF Mono', 'Cascadia Code', monospace"
    fontSize: 11px
    color: "{colors.code-text}"
  badge:
    fontSize: 11px
    fontWeight: 500
  stat-number:
    fontSize: 24px
    fontWeight: 600
    color: "{colors.primary}"
  stat-label:
    fontSize: 11px
    color: "{colors.ink-muted}"
  modal-code:
    fontFamily: "'SF Mono', 'Cascadia Code', monospace"
    fontSize: 11px
    color: "{colors.code-text}"
  filter-label:
    fontSize: 12px
    color: "{colors.ink-muted}"
  filter-input:
    fontSize: 12px
    color: "{colors.ink}"
  action-button:
    fontSize: 12px
    fontWeight: 500

spacing:
  base: 4px
  xxs: 4px
  xs: 8px
  sm: 12px
  md: 16px
  lg: 20px
  xl: 24px
  section: 20px

shadows:
  modal-backdrop:
    background: "rgba(0, 0, 0, .7)"
  modal-content:
    border: "1px solid {colors.hairline}"
    borderRadius: 8px

layout:
  maxWidth: "100%"
  pagePadding: 20px
  content:
    statGrid: "repeat(auto-fit, minmax(160px, 1fr))"
    statGap: 10px
    filterGap: 10px
    actionBarGap: 10px

components:
  tab-bar:
    description: "Top navigation tabs for switching between 对账 / Cron状态 / API日志 panels"
    display: "flex"
    gap: 4px
    marginBottom: 16px

  tab-default:
    background: "{colors.surface-2}"
    border: "1px solid {colors.hairline}"
    color: "{colors.ink-tab-inactive}"
    padding: "8px 18px"
    borderRadius: "6px 6px 0 0"
    cursor: "pointer"
    fontSize: 13px

  tab-active:
    background: "{colors.surface-1}"
    color: "{colors.ink-tab-active}"
    borderBottomColor: "{colors.surface-1}"

  panel:
    display: "none"
    display-active: "block"

  table:
    width: "100%"
    borderCollapse: "collapse"
    fontSize: 12px

  table-row:
    borderBottom: "1px solid {colors.hairline}"

  table-header-cell:
    background: "{colors.surface-1}"
    color: "{colors.ink-muted}"
    fontWeight: 600
    position: "sticky"
    top: 0
    padding: "6px 10px"

  table-data-cell:
    padding: "6px 10px"
    whiteSpace: "nowrap"

  table-row-hover:
    background: "{colors.surface-3}"

  table-row-selected:
    background: "{colors.surface-4}"

  badge:
    display: "inline-block"
    padding: "2px 8px"
    borderRadius: 12px
    fontSize: 11px
    fontWeight: 500

  stat-card:
    background: "{colors.surface-1}"
    border: "1px solid {colors.hairline}"
    borderRadius: 6px
    padding: 12px

  filter-select:
    background: "{colors.surface-2}"
    border: "1px solid {colors.hairline}"
    color: "{colors.ink}"
    padding: "4px 10px"
    borderRadius: 4px
    fontSize: 12px

  filter-input:
    "@inherit": "filter-select"

  pagination-button:
    background: "{colors.surface-2}"
    border: "1px solid {colors.hairline}"
    color: "{colors.ink}"
    padding: "4px 12px"
    borderRadius: 4px
    cursor: "pointer"
    fontSize: 12px

  pagination-button-disabled:
    opacity: 0.4
    cursor: "default"

  action-button:
    background: "#238636"
    border: "none"
    color: "#ffffff"
    padding: "6px 16px"
    borderRadius: 4px
    cursor: "pointer"
    fontSize: 12px
    fontWeight: 500

  action-button-disabled:
    opacity: 0.4
    cursor: "default"

  modal:
    display: "none"
    position: "fixed"
    top: 0
    left: 0
    width: "100%"
    height: "100%"
    background: "{colors.modal-backdrop}"
    zIndex: 1000

  modal-show:
    display: "flex"
    alignItems: "center"
    justifyContent: "center"

  modal-content:
    background: "{colors.surface-1}"
    border: "1px solid {colors.hairline}"
    borderRadius: 8px
    maxWidth: 800px
    width: "90%"
    maxHeight: "80vh"
    overflow: "auto"
    padding: 20px

  checkbox:
    width: 14px
    height: 14px
    cursor: "pointer"
    accentColor: "{colors.checkbox-accent}"

  expand-btn:
    background: "none"
    border: "none"
    color: "{colors.primary}"
    cursor: "pointer"
    fontSize: 11px
    textDecoration: "underline"

  result-msg:
    padding: "8px 14px"
    borderRadius: 4px
    margin: "10px 0"
    fontSize: 12px

  result-msg-ok:
    background: "{colors.result-ok-bg}"
    border: "1px solid {colors.result-ok-border}"
    color: "{colors.result-ok-text}"

  result-msg-err:
    background: "{colors.result-err-bg}"
    border: "1px solid {colors.result-err-border}"
    color: "{colors.result-err-text}"

design_rules:
  color_semantics:
    - "Green (#3fb950) = success, ok, consistent (badge-ok)"
    - "Red (#f85149) = error, inconsistent, abnormal (badge-err)"
    - "Amber (#d29922) = warning, pending, in-progress (badge-warn)"
    - "Blue (#58a6ff) = interactive elements, headings, informational (badge-init, stat numbers)"
    - "DO NOT use green for non-success states"
    - "DO NOT use red for non-error states"
    - "Color is semantic, not decorative"

  layout_rules:
    - "Body padding = 20px"
    - "No fixed widths — use max-width 100%"
    - "Stat grid auto-fits min 160px cards"
    - "Tables fill full width, no horizontal scroll on content columns"
    - "Code columns (单号/物流号) max-width 300px with text-overflow ellipsis"

  component_rules:
    - "Don't add animations or transitions"
    - "Don't add icons beyond emoji in table headers"
    - "Don't add third-party CSS/font libraries"
    - "Don't add hover effects beyond background change"
    - "Modals are the only overlay pattern — no side panels, no dropdowns"
    - "Action bar buttons use green (#238636), not blue"

  content_rules:
    - "Chinese first — all labels, headers, button text in Chinese"
    - "Technical data (trade numbers, IDs, JSON) in green monospace"
    - "Timestamps in muted color (ink-muted)"
    - "Empty states use italic muted text centered in cell"
    - "Truncated text uses CSS text-overflow ellipsis, not JS-based truncation"

  data_display:
    - "Table columns order: ID → 平台单号 → 蓝盟 state → 桥DB state → 吉客云 state → 三端一致 → 吉客云单号 → (关联单号) → 物流单号 → 错误/备注 → 更新于"
    - "Badge colors directly map to operational meaning, not raw enum values"
    - "Three-end consistency displayed as badge: Green=一致, Red=不一致"

do:
  - "Use GitHub Dark (#0d1117) as the base canvas"
  - "Use semantic badges for all state/status display"
  - "Put tables at full width with sticky headers"
  - "Use monospace green for any technical identifier"
  - "Keep all text left-aligned (no centered table data)"
  - "Use dark surface cards for stat aggregation"
  - "Add scrollable modal with monospace code display for JSON/body viewing"

dont:
  - "Don't use light theme"
  - "Don't add charts or visualizations beyond stat cards"
  - "Don't add pagination with page numbers — use prev/next buttons only"
  - "Don't use colored backgrounds on table rows (only hover/selected)"
  - "Don't add user avatar/portrait — this is an ops tool, not a social app"
  - "Don't add real-time WebSocket updates — poll via manual refresh or cron schedule"
  - "Don't add drag-and-drop or reordering"
  - "Don't add right-click context menus"

responsive:
  statGrid: "1 column on < 480px, multi-column on wider"
  tables: "horizontal scroll on mobile if necessary"
  filterBar: "wrap on narrow viewports"
  modal: "90% width with 20px padding on all viewports"
