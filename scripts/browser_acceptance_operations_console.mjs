#!/usr/bin/env node
/**
 * Repeatable browser acceptance for the primary views.
 *
 * This replaces the hand-driven Playwright runs whose only trace was the
 * git-ignored .playwright-cli/ logs: the checks were real, but nobody could
 * re-run them, so every regression needed a human at a keyboard.
 *
 * Start the stack first, then run this:
 *
 *     ./start.sh --mock
 *     npm --prefix operations-console run acceptance
 *
 * It runs only against the isolated mock stack. Most checks are read-only; the
 * Investigator check explicitly starts one fake-model run so the persisted
 * evidence/activity/report flow is exercised. It never touches a notification
 * send path or a real model service.
 *
 * Since F23 it also covers what only a browser can see: that a reload stays on
 * the current view, that a deep link opens directly, and that the back button
 * moves between views instead of leaving the app. Those three were unaddressed
 * for the whole life of the project and are the defects that started F23.
 *
 * Env:
 *   ACCEPTANCE_URL    default http://127.0.0.1:8100
 *   ACCEPTANCE_WIDTH  default 800 (the documented no-overflow width)
 *   ACCEPTANCE_HEAD   set to 1 to watch it run in a headed browser
 */
import { createRequire } from "node:module";

const require = createRequire(new URL("../operations-console/package.json", import.meta.url));
const { chromium } = require("playwright");

const URL_UNDER_TEST = process.env.ACCEPTANCE_URL ?? "http://127.0.0.1:8100";
const WIDTH = Number(process.env.ACCEPTANCE_WIDTH ?? 800);
const HEIGHT = 900;
const SETTLE_MS = 1200;

const VIEWS = [
  { key: "incidents", label: "Incidents", path: "/incidents" },
  { key: "alerts", label: "告警", path: "/alerts" },
  { key: "automation", label: "Automation", path: "/automation/groups" },
  { key: "notifications", label: "通知", path: "/notifications/policies" },
  { key: "analytics", label: "Analytics", path: "/analytics" },
  { key: "settings", label: "系统设置", path: "/settings/sources" },
];

/**
 * One control per view that changes only what is displayed.
 *
 * This is the thin end of the interaction coverage SYSTEM_SPEC §15 says does
 * not otherwise exist: no unit test renders a component, so without these the
 * wiring behind a click is checked by nobody.
 */
const INTERACTIONS = [
  {
    key: "incidents",
    label: "SLA 风险视图写入 URL，详情六页签保持可用",
    async run(page) {
      const risk = page.getByRole("button", { name: "SLA 风险" });
      if ((await risk.count()) === 0) return "找不到 SLA 风险系统视图";
      await risk.click();
      await page.waitForTimeout(600);
      if (new URL(page.url()).searchParams.get("view") !== "SLA_AT_RISK") {
        return "系统视图没有写入 URL";
      }
      const row = page.locator(".incident-queue-card").first();
      if ((await row.count()) === 0) {
        return (await page.locator(".empty").count()) > 0
          ? "skipped (no SLA-risk occurrence right now)"
          : "队列既没有对象也没有空态说明";
      }
      await row.click();
      await page.waitForTimeout(600);
      if (!/\/incidents\/\d+/.test(new URL(page.url()).pathname)) {
        return "选择 occurrence 后详情 URL 未更新";
      }
      if ((await page.locator("[aria-label='Incident Overview']").count()) === 0) {
        return "详情 URL 没有呈现 Overview";
      }
      const tabs = [
        ["概览", "Overview"],
        ["原始告警", "Occurrence Alerts"],
        ["证据", "Occurrence Evidence"],
        ["AI 调查", "AI Investigator"],
        ["时间线", "Incident Timeline"],
      ];
      for (const [tab, region] of tabs) {
        await page.getByRole("button", { name: tab, exact: true }).click();
        await page.waitForTimeout(120);
        if ((await page.getByRole("region", { name: region, exact: true }).count()) === 0) {
          return `${tab} 页签没有呈现 ${region}`;
        }
      }
      await page.getByRole("button", { name: "处置", exact: true }).click();
      await page.waitForTimeout(120);
      if (new URL(page.url()).hash !== "#actions") {
        return "处置页签没有写入 URL";
      }
      if ((await page.locator("[aria-label='开始人工处理'], [aria-label='人工处置任务'], .incident-readonly-note").count()) === 0) {
        return "处置页没有呈现与当前状态匹配的唯一主流程";
      }
      return null;
    },
  },
  {
    key: "incidents",
    label: "V2 调查交付证据快照、持久活动和结构化报告",
    async run(page) {
      await page.goto(`${URL_UNDER_TEST}/incidents`);
      await page.waitForTimeout(SETTLE_MS);
      const row = page.locator(".incident-queue-card", { hasText: "CheckoutErrorRateHigh" }).first();
      if ((await row.count()) === 0) return "mock 没有可取得指标证据的 CheckoutErrorRateHigh";
      await row.click();
      await page.waitForTimeout(600);
      await page.getByRole("button", { name: "AI 调查", exact: true }).click();
      await page.waitForTimeout(500);
      const panel = page.getByRole("region", { name: "AI Investigator" });
      if ((await panel.count()) !== 1) return "AI 调查页没有 Investigator 主面板";
      const start = panel.getByRole("button", { name: /开始 AI 调查|重新调查当前事件/ });
      if ((await start.count()) !== 1) return "没有统一的调查启动按钮";
      if (await start.isDisabled()) {
        await page.waitForFunction(() => {
          const button = [...document.querySelectorAll("button")]
            .find((item) => /开始 AI 调查|重新调查当前事件/.test(item.textContent ?? ""));
          return button instanceof HTMLButtonElement && !button.disabled;
        }, undefined, { timeout: 15_000 });
      }
      await start.click();
      await page.waitForFunction(() => {
        const panel = document.querySelector('[aria-label="AI Investigator"]');
        const text = panel?.textContent ?? "";
        return text.includes("调查活动") && text.includes("调查报告");
      }, undefined, { timeout: 20_000 });
      const text = await panel.innerText();
      for (const expected of ["证据快照", "调查活动", "调查报告", "结构化输出已校验"]) {
        if (!text.includes(expected)) return `V2 调查结果缺少「${expected}」`;
      }
      if (/\bP[012]\b/.test(text)) return "新调查主面仍泄漏旧 P0/P1/P2 阶段术语";
      return null;
    },
  },
  {
    key: "settings",
    label: "长页面滚到底，侧栏与页脚仍在原位",
    async run(page) {
      // Only a browser sees this one. `.app` was `height: 100vh` while the
      // document scrolled, so on any view taller than the viewport the shell
      // was dragged up: the sidebar's background stopped mid-page and its
      // footer was left stranded on the page background.
      // The sidebar is collapsed away at the documented 800px width, so this
      // one needs the layout it is actually about. Restored before returning.
      await page.setViewportSize({ width: 1280, height: 720 });
      await page.goto(`${URL_UNDER_TEST}/settings/model`);
      await page.waitForTimeout(SETTLE_MS);

      const restore = async () => {
        await page.setViewportSize({ width: WIDTH, height: HEIGHT });
        await page.goto(`${URL_UNDER_TEST}/settings/sources`);
        await page.waitForTimeout(SETTLE_MS);
      };

      const main = page.locator(".app-main");
      if ((await main.count()) === 0) {
        await restore();
        return "找不到主区";
      }
      await main.evaluate((node) => {
        node.scrollTop = node.scrollHeight;
      });
      await page.waitForTimeout(400);

      const docScrolled = await page.evaluate(() => window.scrollY);
      const side = await page.locator(".app-sidebar").boundingBox();
      const foot = await page.locator(".app-sidebar__foot").boundingBox();
      await restore();

      if (docScrolled !== 0) return `文档整体被滚动了 ${docScrolled}px，侧栏会被拖走`;
      if (!side || !foot) return "侧栏或页脚不可见";
      if (foot.y + foot.height > side.y + side.height + 1) {
        return "滚到底之后页脚跑到了侧栏外面";
      }
      if (side.y < -1) return "侧栏被滚出了视口顶部";
      return null;
    },
  },
  {
    key: "alerts",
    label: "选中另一个告警组写入 URL",
    async run(page) {
      const rows = page.locator(".split .card");
      if ((await rows.count()) < 2) return "skipped (fewer than two groups)";
      await rows.nth(1).click();
      await page.waitForTimeout(600);
      const search = new URL(page.url()).searchParams;
      return search.get("incident") ? null : "clicking a group did not put it in the URL";
    },
  },
  {
    key: "alerts",
    label: "Watchdog 状态条收起只占一行，点开才下钻",
    async run(page) {
      const strip = page.locator(".watchdog-strip");
      if ((await strip.count()) === 0) return "the alerts view has no watchdog strip";

      const split = await page.locator(".split").boundingBox();
      const collapsed = await strip.boundingBox();
      if (collapsed.y < split.y) return "the watchdog strip sits above the alert groups";
      if (collapsed.height > 48) {
        return `the collapsed strip is ${Math.round(collapsed.height)}px tall, not one row`;
      }

      const toggle = strip.locator(".watchdog-strip__toggle");
      if ((await toggle.count()) === 0) return "skipped (no watchdog data to drill into)";
      await toggle.click();
      await page.waitForTimeout(600);
      if ((await strip.locator(".watchdog-clusters").count()) === 0) {
        return "clicking the strip did not open the drilldown";
      }
      await toggle.click();
      await page.waitForTimeout(600);
      return (await strip.locator(".watchdog-clusters").count()) === 0
        ? null
        : "the drilldown did not close again";
    },
  },
  {
    key: "alerts",
    label: "已恢复告警组默认收在折叠区里，点开才出现",
    async run(page) {
      const fold = page.locator(".recovered-fold");

      /*
       * 自带 mock 里 source-b 的 transient probe 会在 ~30-40s 后恢复
       * （mock_alertmanager.TRANSIENT_POLL_LIMIT + grace 0 + 10s interval），
       * 冷启动跑到这里时它可能还在 firing，所以等它——但**只在 mock 上等**，
       * 别让真实栈白等一分钟。在 mock 上等不到就是失败：这条断言以前永久 skip，
       * 就是因为没有任何东西保证已恢复组存在。
       */
      const onBundledMock =
        (await page.locator(".source-filter__item", { hasText: "source-b" }).count()) > 0;
      if (onBundledMock) {
        try {
          await fold.first().waitFor({ state: "attached", timeout: 60_000 });
        } catch {
          return (
            "the bundled mock produced no recovered group in 60s — is the transient " +
            "probe still firing, or does an aggregation rule split it away from the " +
            "active groups in this bucket?"
          );
        }
      }
      if ((await fold.count()) === 0) return "skipped (no recovered groups right now)";

      const mainRows = page.locator(".pane--list > .rows");
      const toggle = fold.locator(".recovered-fold__row");
      const isExpanded = () => fold.locator(".rows--recovered").count().then((n) => n > 0);

      // 折叠区必须排在主列表之后。
      const mainBox = await mainRows.boundingBox();
      const foldBox = await fold.boundingBox();
      if (mainBox && foldBox.y < mainBox.y) {
        return "the recovered fold sits above the active groups";
      }

      // R1: 已恢复的组不许出现在主列表里，哪怕折叠区也在场。
      if ((await mainRows.locator(".pill--recovered").count()) !== 0) {
        return "a recovered group is still listed among the active ones";
      }

      // 默认收起——但主列表为空时选中项会落到折叠区里的第一条，那时自动展开才是
      // 对的（R6），所以这条只在还有活动组可选的情况下成立。
      const hasActiveRows = (await mainRows.locator(".card").count()) > 0;
      if (hasActiveRows && (await isExpanded())) {
        return "the recovered groups are already expanded before anyone clicked";
      }
      if (!hasActiveRows && !(await isExpanded())) {
        return "nothing is active, yet the recovered fold stayed shut on the selected group";
      }

      // 从这里起两条分支合流：收起必须只占一行，展开必须给出卡片，两下点击都要生效。
      if (await isExpanded()) {
        await toggle.click();
        await page.waitForTimeout(600);
        if (await isExpanded()) return "clicking the fold did not close it";
      }
      const collapsed = await fold.boundingBox();
      if (collapsed.height > 48) {
        return `the collapsed fold is ${Math.round(collapsed.height)}px tall, not one row`;
      }
      await toggle.click();
      await page.waitForTimeout(600);
      return (await fold.locator(".rows--recovered .card").count()) > 0
        ? null
        : "clicking the fold did not reveal the recovered groups";
    },
  },
  {
    key: "analytics",
    label: "确定性汇总展示公式分母，范围与筛选写入 URL",
    async run(page) {
      await page.goto(`${URL_UNDER_TEST}/analytics`);
      await page.waitForTimeout(SETTLE_MS);
      const main = page.locator(".analytics-page");
      if ((await main.count()) !== 1) return "Analytics 主页面没有呈现";
      const text = await main.innerText();
      for (const expected of [
        "数字来自持久汇总，不由模型撰写",
        "压缩率",
        "(告警实例 − 事件) / 告警实例",
        "通知投递",
        "AI 调查",
      ]) {
        if (!text.includes(expected)) return `Analytics 缺少「${expected}」`;
      }
      await page.getByRole("button", { name: "24 小时" }).click();
      await page.getByLabel("信号严重度").selectOption("warning");
      await page.waitForTimeout(600);
      const selection = new URL(page.url()).searchParams;
      if (selection.get("range") !== "24h" || selection.get("severity") !== "warning") {
        return `范围或严重度未写入 URL：${selection.toString()}`;
      }
      return null;
    },
  },
  {
    key: "analytics",
    label: "按结论筛选写入 URL 且列表随之收窄",
    async run(page) {
      const historyLink = page.getByRole("link", { name: "查看事件历史" });
      if ((await historyLink.count()) !== 1) return "Analytics 没有历史下钻入口";
      await historyLink.click();
      await page.waitForTimeout(SETTLE_MS);
      const rows = page.locator(".pane--list .card");
      const before = await rows.count();
      if (before === 0) {
        // Only a genuine empty history is acceptable here, and it has to say so:
        // an empty table with no explanation reads as a broken page (R7 / D12).
        const empty = page.locator(".empty", { hasText: "历史从本次升级之后开始累积" });
        return (await empty.count()) > 0
          ? "skipped (no occurrence history yet)"
          : "history is empty and the page does not explain why";
      }

      const select = page.locator('select[aria-label="按处理结论筛选"]');
      if ((await select.count()) === 0) return "the history view has no conclusion filter";
      await select.selectOption("CLOSED");
      await page.waitForTimeout(800);

      const search = new URL(page.url()).searchParams;
      if (search.get("conclusion") !== "CLOSED") {
        return "filtering by conclusion did not reach the URL";
      }
      // Everything the mock produces is unhandled, so CLOSED must narrow to none.
      const narrowed = await rows.count();
      if (narrowed >= before) {
        return `filtering did not narrow the list (${before} -> ${narrowed})`;
      }

      await select.selectOption("");
      await page.waitForTimeout(800);
      // Polling may seal another occurrence while this scenario is running;
      // clearing must restore everything seen before, but may legitimately add
      // newer rows.
      return (await rows.count()) >= before
        ? null
        : "clearing the filter did not restore the list";
    },
  },
  {
    key: "automation",
    label: "指标模板页签写入 URL 且可后退",
    async run(page) {
      // F27 put metric templates on a tab rather than a sixth sidebar entry, so
      // the tab has to behave like navigation: addressable and undoable.
      const previousPath = new URL(page.url()).pathname;
      const tab = page.locator(".domain-tabs button", { hasText: "指标模板" });
      if ((await tab.count()) === 0) return "指标模板页签不存在";
      await tab.click();
      await page.waitForTimeout(400);
      if (!page.url().includes("/automation/metrics")) {
        return `switching to the metrics tab did not reach /automation/metrics (${page.url()})`;
      }
      const table = page.locator(".metric-template-table");
      if ((await table.count()) === 0) return "指标模板列表没有渲染";
      // Shipped switched off: an unverified query that draws nothing reads as a
      // broken feature rather than as a wrong metric name.
      //
      // Scoped to the shipped rows only. It used to count every ticked box in
      // the table, which said "built-in" and measured "all" — harmless while
      // built-ins were the only templates, and wrong the moment importing let a
      // user deliberately enable one of their own.
      const checked = await page
        .locator('[data-testid="template-row"][data-builtin="1"] input[type=checkbox]:checked')
        .count();
      if (checked !== 0) return `内置模板出厂应当全部关闭，实际勾选了 ${checked} 条`;
      await page.goBack();
      await page.waitForTimeout(400);
      return new URL(page.url()).pathname === previousPath
        ? null
        : `back from the metrics tab did not return to ${previousPath} (${page.url()})`;
    },
  },
  {
    key: "alerts",
    label: "告警详情有指标区并说明失败原因",
    async run(page) {
      const section = page.locator('[data-testid="metric-evidence"]');
      await page.waitForTimeout(1200);
      if ((await section.count()) === 0) return "告警详情没有指标区";
      // The mock has no Thanos address on its sources, so the honest outcome is
      // a *named* failure — never a blank panel, and never "failed to load".
      const text = await section.first().innerText();
      if (!text.includes("指标")) return "指标区标题缺失";
      // Structural rather than string-matching: the property is "never a blank
      // panel", and pinning exact wording here would make this test fail on a
      // copy edit while missing the thing it is meant to catch. (The first
      // version listed a handful of phrases and went red on a perfectly good
      // "指标存在，但这组标签下没有数据".)
      const curves = await section.locator(".metric-curve").count();
      const failures = await section.locator(".metric-evidence__failures li").count();
      const emptyNote = text.includes("没有可画的指标") ? 1 : 0;
      if (curves + failures + emptyNote === 0) {
        return `指标区既没有曲线也没有可读的原因：${text.slice(0, 160)}`;
      }
      // Whatever is shown must be self-explanatory, never a bare "failed".
      if (curves === 0 && failures > 0) {
        const first = await section.locator(".metric-evidence__failures li").first().innerText();
        if (first.trim().length < 6) return `失败项没有可读文案：${first}`;
      }
      return null;
    },
  },
  {
    key: "alerts",
    label: "旧 Alert-root AI 面已按裁决退役",
    async run(page) {
      const retiredPanels = page.locator(".alert-investigation, .suggested-queries");
      const retiredButtons = page.getByRole("button", {
        name: /让 AI 看看|让 AI 找相关指标/,
      });
      return (await retiredPanels.count()) === 0 && (await retiredButtons.count()) === 0
        ? null
        : "已退役的调查入口仍在当前界面";
    },
  },
  {
    key: "automation",
    label: "标签窗口切换不清空表单",
    async run(page) {
      const select = page.locator('select[aria-label="标签发现窗口"]');
      if ((await select.count()) === 0) return "skipped (no rule editor)";
      const nameInput = page.locator(".rule-basics input").first();
      const before = await nameInput.inputValue();
      await select.selectOption("24");
      await page.waitForTimeout(800);
      const after = await nameInput.inputValue();
      return before === after ? null : `changing the window rewrote the draft name (${before} -> ${after})`;
    },
  },
  {
    key: "notifications",
    label: "切 tab 改变路径且可后退",
    async run(page) {
      await page.locator(".domain-tabs button", { hasText: "投递记录" }).click();
      await page.waitForTimeout(600);
      if (!page.url().includes("/notifications/deliveries")) {
        return "switching tab did not change the path";
      }
      await page.goBack();
      await page.waitForTimeout(600);
      return page.url().includes("/notifications/policies")
        ? null
        : "back from a tab did not return to the previous tab";
    },
  },
  {
    key: "notifications-channel-kind",
    label: "新建通道可选渠道类型且表单随之改变",
    async run(page) {
      // Read-only: it picks a kind and reads the form. It never saves and never
      // touches 发送测试消息, which is the only control that reaches outside.
      await page.goto(`${URL_UNDER_TEST}/notifications/channels`, {
        waitUntil: "domcontentloaded",
      });
      await page.waitForTimeout(SETTLE_MS);
      const kinds = page.locator('input[name="channel-kind"]');
      if ((await kinds.count()) < 3) return "fewer than three channel kinds offered";

      await page.locator(".check-card", { hasText: "邮件（SMTP）" }).click();
      await page.waitForTimeout(400);
      const smtpFields = await page.locator(".editor-section", { hasText: "SMTP 主机" }).count();
      if (smtpFields === 0) return "choosing SMTP did not bring up its own fields";

      await page.locator(".check-card", { hasText: "通用 Webhook" }).click();
      await page.waitForTimeout(400);
      const warned = await page
        .locator(".catchall-warning", { hasText: "最多 2 条确定性指标值" })
        .count();
      if (warned === 0) return "the generic webhook form does not disclose its bounded evidence payload";

      const guidance = await page.locator(".requirement-list li").count();
      return guidance > 0 ? null : "no guidance shown for the selected kind";
    },
  },
  {
    key: "notifications-channel-roundtrip",
    label: "已保存通道完整回填非敏感配置",
    async run(page) {
      await page.goto(`${URL_UNDER_TEST}/notifications/channels`, {
        waitUntil: "domcontentloaded",
      });
      await page.waitForTimeout(SETTLE_MS);
      const channel = page.locator(".resource-row", { hasText: "本地验收 Webhook" });
      if ((await channel.count()) !== 1) return "mock 缺少通知编辑回填 fixture";
      await channel.click();
      await page.waitForTimeout(500);

      const editor = page.locator(".resource-editor");
      const endpoint = editor.getByRole("textbox", { name: "HTTPS 端点", exact: true });
      const timeout = editor.getByRole("spinbutton", { name: "超时（秒）", exact: true });
      const endpointCount = await endpoint.count();
      const timeoutCount = await timeout.count();
      if (endpointCount !== 1 || timeoutCount !== 1) {
        return `选择已保存 Webhook 后字段数量异常（endpoint=${endpointCount}, timeout=${timeoutCount}, url=${page.url()}）`;
      }
      if ((await endpoint.inputValue()) !== "https://hooks.example.invalid/workbench-acceptance") {
        return "已保存 Webhook 地址没有回填";
      }
      if ((await timeout.inputValue()) !== "12") return "已保存超时值没有回填";
      return null;
    },
  },
  {
    key: "settings",
    label: "选中来源写入 ?source= 且详情跟随",
    async run(page) {
      // Settings now has a second resource split for model services. Keep this
      // assertion anchored to the first/data-source split so a model heading
      // cannot satisfy or make the source detail selector ambiguous.
      const sourceSplit = page.locator(".settings-page > .settings-split").first();
      const rows = sourceSplit.locator(".resource-row");
      if ((await rows.count()) === 0) return "skipped (no sources configured)";
      const name = (await rows.first().locator("strong").textContent())?.trim();
      await rows.first().click();
      await page.waitForTimeout(600);
      const selected = new URL(page.url()).searchParams.get("source");
      if (!selected) return "selecting a source did not put it in the URL";
      const heading = (
        await sourceSplit.locator(".editor-head h2").textContent()
      )?.trim();
      return heading === name ? null : `URL says ${selected} but the editor shows ${heading}`;
    },
  },
  {
    key: "settings",
    label: "模型服务是自己的页签，不是数据源下面的一段",
    async run(page) {
      const tabs = page.locator('.domain-tabs[aria-label="系统设置"]');
      if ((await tabs.count()) === 0) return "系统设置没有页签";
      // On the sources tab the model panel must not be on screen at all — the
      // whole point of this change is that it stopped being an accessory to the
      // data source list.
      if ((await page.getByRole("region", { name: "模型服务" }).count()) > 0) {
        return "数据源页签上仍然渲染了模型服务区块";
      }
      await tabs.locator("button", { hasText: "模型服务" }).click();
      await page.waitForTimeout(800);
      if (!page.url().includes("/settings/model")) {
        return `切到模型服务没有写进 URL：${page.url()}`;
      }
      await page.goBack();
      await page.waitForTimeout(800);
      return page.url().includes("/settings/sources")
        ? null
        : `后退没有回到数据源页签：${page.url()}`;
    },
  },
  {
    key: "settings",
    label: "模型服务默认不出站并明示数据出境范围",
    async run(page) {
      // Navigate rather than assume: the check above leaves the page on 数据源,
      // and a deep link is exactly what the tab now makes possible.
      await page.goto(`${URL_UNDER_TEST}/settings/model`);
      await page.waitForTimeout(SETTLE_MS);
      const panel = page.getByRole("region", { name: "模型服务" });
      if ((await panel.count()) !== 1) return "model service panel is missing";
      const warning = await panel
        .locator(".model-egress-warning", { hasText: "指标数据和历史处置记录" })
        .count();
      if (warning !== 1) return "model service data-egress disclosure is missing";
      // Start from a fresh form rather than whatever the mock database happens
      // to hold: an earlier version of this check asserted "the form is empty"
      // and started failing the moment a channel existed — testing the fixture,
      // not the rule.
      const add = panel.getByRole("button", { name: "添加模型服务" });
      if ((await add.count()) === 0) return "没有「添加模型服务」按钮";
      await add.click();
      await page.waitForTimeout(600);

      const save = panel.getByRole("button", { name: /保存/ }).first();
      if ((await save.count()) === 0) return "新建表单没有保存按钮";
      if (!(await save.isDisabled())) {
        return "空表单也能保存——名称、模型或写入型 API key 的必填被绕过了";
      }
      const modelPicker = panel.getByLabel("模型名");
      if ((await modelPicker.count()) !== 1) return "首次配置没有模型选择器";
      if ((await modelPicker.locator("option", { hasText: "gpt-5.5" }).count()) !== 1) {
        return "首次保存前没有 OpenAI 推荐模型";
      }
      if ((await modelPicker.locator("option", { hasText: "fake-planner" }).count()) !== 0) {
        return "本地 Fake 内部名称泄漏到模型选择器";
      }
      if ((await panel.getByRole("button", { name: "刷新账号可用模型" }).count()) !== 0) {
        return "本地安全模式仍提供远端模型目录刷新";
      }
      return null;
    },
  },
  {
    key: "settings",
    label: "Grafana 测试门只表现为入口禁用，不引入状态机词汇",
    async run(page) {
      await page.goto(`${URL_UNDER_TEST}/settings/sources`);
      await page.waitForTimeout(SETTLE_MS);
      const sourceSplit = page.locator(".settings-page > .settings-split").first();
      const rows = sourceSplit.locator(".resource-row");
      if ((await rows.count()) === 0) return "skipped (no sources configured)";
      await rows.first().click();
      await page.waitForTimeout(600);

      const panel = page.locator("details", { hasText: "Grafana（选填）" }).first();
      if ((await panel.count()) === 0) return "数据源详情里没有 Grafana 折叠区";
      await panel.locator("summary").click();
      await page.waitForTimeout(400);

      // The whole vocabulary rule: a data source saves and is in effect. Draft
      // and activation belong to the notification and model pages.
      const text = (await panel.textContent()) ?? "";
      for (const banned of ["DRAFT", "ACTIVE", "草稿", "候选配置", "应用配置"]) {
        if (text.includes(banned)) return `Grafana 区块出现了状态机词汇「${banned}」`;
      }
      return null;
    },
  },
  {
    key: "settings",
    label: "从 Grafana 导入：四类候选、勾选、落成带来源徽标的模板",
    async run(page) {
      await page.goto(`${URL_UNDER_TEST}/settings/sources`);
      await page.waitForTimeout(SETTLE_MS);
      const sourceSplit = page.locator(".settings-page > .settings-split").first();
      const rows = sourceSplit.locator(".resource-row", { hasText: "source-a" });
      if ((await rows.count()) === 0) return "skipped (source-a not configured)";
      await rows.first().click();
      await page.waitForTimeout(600);

      const panel = page.locator("details", { hasText: "Grafana（选填）" }).first();
      await panel.locator("summary").click();
      await page.waitForTimeout(400);

      // The seed already configured and tested it, so the gate should be open.
      const open = page.getByTestId("grafana-import-open");
      if ((await open.count()) === 0) return "没有「从 dashboard 导入」按钮";
      if (await open.isDisabled()) {
        return "导入入口仍然禁用——seed 的测试没有成功，或者门读错了状态";
      }
      await open.click();
      await page.waitForTimeout(600);

      const drawer = page.getByTestId("grafana-import-drawer");
      if ((await drawer.count()) === 0) return "导入抽屉没有打开";
      const dashboards = drawer.getByTestId("grafana-dashboard-list").locator("button");
      if ((await dashboards.count()) === 0) return "没有列出任何 dashboard";
      const classificationFixture = dashboards.filter({ hasText: "Node health" });
      await ((await classificationFixture.count()) > 0
        ? classificationFixture.first()
        : dashboards.first()).click();
      await page.waitForTimeout(1500);

      const badges = await drawer.getByTestId("candidate-badge").allTextContents();
      if (badges.length === 0) return "没有解析出任何候选";
      // The mock dashboard is built so these appear together: the Loki panel and
      // the table panel are unusable for two different reasons, and the variable
      // panel needs a decision.
      //
      // Unchanged candidates are deliberately not listed, so on a stack whose
      // database already holds this import the list is legitimately shorter.
      // Say so rather than failing — re-importing an unchanged dashboard showing
      // nothing is the correct behaviour, not a regression.
      if (
        !badges.some((item) => item.includes("需你决定")) &&
        !badges.some((item) => item.includes("直接可用"))
      ) {
        return `skipped (这个 dashboard 已经导入过了，只剩：${badges.join(" / ")})`;
      }
      for (const expected of ["直接可用", "用不了"]) {
        if (!badges.some((item) => item.includes(expected))) {
          if (expected === "直接可用") {
            // A previous acceptance run imports the only directly-usable row.
            // Re-import correctly omits that unchanged target while still
            // listing undecided and unusable rows, so the old `NEEDS_DECISION`
            // shortcut above is not enough to make this scenario repeatable.
            await page.goto(`${URL_UNDER_TEST}/automation/metrics`);
            await page.waitForTimeout(SETTLE_MS);
            const origins = await page.getByTestId("template-origin").count();
            if (origins > 0) return null;
          }
          return `候选徽标里没有「${expected}」：${badges.join(" / ")}`;
        }
      }

      const budget = await drawer.getByTestId("grafana-curve-budget").textContent();
      if (!budget?.includes("最多画")) return "确认区没有说明一条告警最多画几条";

      // Tick the first candidate that can actually be submitted.
      const boxes = drawer.getByTestId("grafana-candidate-list").locator('input[type="checkbox"]');
      let ticked = 0;
      for (let index = 0; index < (await boxes.count()); index += 1) {
        const box = boxes.nth(index);
        if (await box.isDisabled()) continue;
        await box.check();
        ticked += 1;
        if (ticked === 1) break;
      }
      if (ticked === 0) return "没有任何候选可以勾选";

      const confirm = drawer.getByTestId("grafana-import-confirm");
      if (await confirm.isDisabled()) return "勾选之后确认按钮仍然禁用";
      await confirm.click();
      await page.waitForTimeout(1500);
      const message = await drawer.getByTestId("grafana-import-message").textContent();
      if (!message?.includes("已导入")) return `确认没有成功：${message}`;

      // The tab is `metrics` (RULE_TABS), not `templates` — an unknown tab
      // falls back and renders no table at all.
      await page.goto(`${URL_UNDER_TEST}/automation/metrics`);
      await page.waitForTimeout(SETTLE_MS);
      const origins = await page.getByTestId("template-origin").count();
      return origins > 0 ? null : "指标模板页没有出现「来自 Grafana」的模板";
    },
  },
  {
    key: "settings",
    label: "撤销参照线后保留导入模板的来源范围",
    async run(page) {
      await page.goto(`${URL_UNDER_TEST}/automation/metrics`);
      await page.waitForTimeout(SETTLE_MS);
      const row = page.locator('[data-testid="template-row"]', { hasText: "Filesystem used" }).first();
      if ((await row.count()) === 0) return "skipped (mock Grafana template was not imported)";

      const rowText = await row.innerText();
      if (rowText.includes("参照线") || rowText.includes("大了不好") || rowText.includes("小了不好")) {
        return `指标模板仍暴露已撤销的参照线：${rowText.slice(0, 180)}`;
      }
      await row.getByRole("button", { name: "设置适用范围" }).click();
      await page.waitForTimeout(500);

      const editor = page.locator(".metric-template-editor-row").first();
      const editorText = await editor.innerText();
      if (!editorText.includes("source-a")) {
        return `导入模板没有默认限定到 origin 来源：${editorText.slice(0, 160)}`;
      }
      return editorText.includes("参照线") ? "来源范围编辑器仍包含参照线" : null;
    },
  },
];

const failures = [];
const noted = [];

function fail(view, message) {
  failures.push(`[${view}] ${message}`);
}

async function assertNoHorizontalOverflow(page, view) {
  const overflow = await page.evaluate(() => {
    const doc = document.documentElement;
    // 1px of slack absorbs subpixel rounding in the layout engine.
    if (doc.scrollWidth <= doc.clientWidth + 1) return null;
    const offenders = [...document.querySelectorAll("*")]
      .filter((el) => el.getBoundingClientRect().right > doc.clientWidth + 1)
      .slice(0, 5)
      .map((el) => {
        const cls = typeof el.className === "string" ? el.className : "";
        return `${el.tagName.toLowerCase()}${cls ? `.${cls.trim().split(/\s+/).join(".")}` : ""}`;
      });
    return { scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth, offenders };
  });
  if (overflow) {
    fail(
      view,
      `horizontal overflow: scrollWidth ${overflow.scrollWidth} > clientWidth ` +
        `${overflow.clientWidth}; widest: ${overflow.offenders.join(", ") || "unknown"}`,
    );
  }
}

async function assertNoSecretEchoed(page, view) {
  const echoed = await page.evaluate(() =>
    [...document.querySelectorAll("input")]
      .filter((el) => el.type === "password" && el.value !== "")
      .map((el) => el.name || el.id || "<unnamed>"),
  );
  if (echoed.length) {
    fail(view, `password input rendered with a value: ${echoed.join(", ")}`);
  }
}

async function assertKeyboardFocus(page) {
  await page.goto(`${URL_UNDER_TEST}/alerts`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(SETTLE_MS);
  await page.evaluate(() => document.activeElement?.blur());
  await page.keyboard.press("Tab");
  const focus = await page.evaluate(() => {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement)) return null;
    const style = getComputedStyle(active);
    return {
      tag: active.tagName,
      outline: style.outlineStyle,
      width: style.outlineWidth,
    };
  });
  if (!focus || focus.tag === "BODY" || focus.outline === "none" || focus.width === "0px") {
    fail("accessibility", `keyboard focus is not visibly exposed: ${JSON.stringify(focus)}`);
    return;
  }
  noted.push("可访问性 ok · Tab 键可达且 focus-visible 清晰");
}

async function assertDarkScheme(page) {
  await page.emulateMedia({ colorScheme: "light" });
  const light = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue("--bg"),
  );
  await page.emulateMedia({ colorScheme: "dark" });
  const dark = await page.evaluate(() => ({
    token: getComputedStyle(document.documentElement).getPropertyValue("--bg"),
    matches: matchMedia("(prefers-color-scheme: dark)").matches,
  }));
  if (!dark.matches || dark.token.trim() === light.trim()) {
    fail("appearance", `dark scheme did not change the surface token (${light} -> ${dark.token})`);
  } else {
    noted.push("外观 ok · dark scheme 使用独立颜色 token");
  }
  await page.emulateMedia({ colorScheme: "light" });
}

/**
 * The three things F23 exists for. Before it, the current view lived in React
 * state: a reload went back to 告警 and the back button left the app.
 */
async function assertAddressableNavigation(page) {
  const previous = "navigation";

  // Deep link: type the address of a view that is not the default and land there.
  await page.goto(`${URL_UNDER_TEST}/settings`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(SETTLE_MS);
  if (!new URL(page.url()).pathname.startsWith("/settings")) {
    fail(previous, `deep link to /settings ended at ${page.url()}`);
  }
  const settingsTitle = (await page.locator(".topbar__title").textContent())?.trim();
  if (settingsTitle !== "系统设置") {
    fail(previous, `deep link to /settings rendered "${settingsTitle}"`);
  }

  // Reload: stay put rather than going home.
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(SETTLE_MS);
  const afterReload = (await page.locator(".topbar__title").textContent())?.trim();
  if (afterReload !== "系统设置") {
    fail(previous, `reload on /settings landed on "${afterReload}"`);
  }

  // Back: move between views inside the app.
  await page.locator(".side-nav button", { hasText: "Automation" }).click();
  await page.waitForTimeout(SETTLE_MS);
  await page.goBack();
  await page.waitForTimeout(SETTLE_MS);
  const afterBack = new URL(page.url()).pathname;
  if (!afterBack.startsWith("/settings")) {
    fail(previous, `back from Automation went to ${afterBack} instead of /settings`);
  }

  // An unreadable path is a stale link, not a dead end.
  await page.goto(`${URL_UNDER_TEST}/nope`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(SETTLE_MS);
  if (!new URL(page.url()).pathname.startsWith("/incidents")) {
    fail(previous, `unknown path did not fall back to /incidents (got ${page.url()})`);
  }

  noted.push("导航 ok · 深链 / 刷新 / 后退 / 未知路径兜底");
}

async function main() {
  const browser = await chromium.launch({ headless: process.env.ACCEPTANCE_HEAD !== "1" });
  const context = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
  const page = await context.newPage();

  let currentView = "startup";
  let platformHealthRequests = 0;
  let legacyHealthRequests = 0;
  page.on("request", (req) => {
    const path = new URL(req.url()).pathname;
    if (path === "/api/v1/platform-health") platformHealthRequests += 1;
    if (path === "/api/health") legacyHealthRequests += 1;
  });
  page.on("console", (msg) => {
    const type = msg.type();
    if (type === "warning" || type === "error") {
      fail(currentView, `console.${type}: ${msg.text()}`);
    }
  });
  page.on("pageerror", (err) => fail(currentView, `uncaught: ${err.message}`));
  page.on("requestfailed", (req) => {
    // A cancelled in-flight poll on teardown is not a defect.
    const reason = req.failure()?.errorText ?? "";
    if (reason.includes("ERR_ABORTED")) return;
    fail(currentView, `request failed: ${req.method()} ${req.url()} (${reason})`);
  });
  page.on("response", (resp) => {
    if (resp.status() >= 500) {
      fail(currentView, `server error: ${resp.status()} ${resp.url()}`);
    }
  });

  try {
    await page.goto(URL_UNDER_TEST, { waitUntil: "domcontentloaded", timeout: 15_000 });
  } catch (err) {
    console.error(
      `\ncannot reach ${URL_UNDER_TEST}\n` +
        `start the stack first:  ./start.sh --mock\n\n${err.message}\n`,
    );
    await browser.close();
    process.exit(2);
  }

  await page.waitForTimeout(SETTLE_MS);

  for (const view of VIEWS) {
    currentView = view.key;
    const entry = page.locator(".side-nav button", { hasText: view.label }).first();
    if ((await entry.count()) === 0) {
      fail(view.key, `sidebar entry "${view.label}" not found`);
      continue;
    }
    await entry.click();
    // The pages fetch on mount; give the poll a beat before asserting.
    await page.waitForTimeout(SETTLE_MS);

    await assertNoHorizontalOverflow(page, view.key);
    await assertNoSecretEchoed(page, view.key);

    for (const interaction of INTERACTIONS.filter((item) =>
      item.key === view.key || item.key.startsWith(`${view.key}-`),
    )) {
      const problem = await interaction.run(page);
      if (problem !== null && !problem.startsWith("skipped")) {
        fail(view.key, `${interaction.label}: ${problem}`);
      } else {
        noted.push(`${view.label} · ${interaction.label}${problem ? ` ${problem}` : ""}`);
      }
    }
    const interaction = null;
    void interaction;
    noted.push(`${view.label} ok`);
  }

  await assertAddressableNavigation(page);
  await assertKeyboardFocus(page);
  await assertDarkScheme(page);
  if (platformHealthRequests === 0) {
    fail("health", "UI did not consume GET /api/v1/platform-health");
  }
  if (legacyHealthRequests !== 0) {
    fail("health", `UI still requested legacy /api/health ${legacyHealthRequests} time(s)`);
  }
  noted.push(
    `平台健康 ok · 单一 /api/v1/platform-health 快照（${platformHealthRequests} 次轮询）`,
  );

  currentView = "teardown";
  await browser.close();

  console.log(`\nbrowser acceptance @ ${URL_UNDER_TEST} (viewport ${WIDTH}x${HEIGHT})`);
  for (const line of noted) console.log(`  - ${line}`);

  if (failures.length) {
    console.error(`\n${failures.length} failure(s):`);
    for (const line of failures) console.error(`  ! ${line}`);
    process.exit(1);
  }
  console.log(
    `\nall ${VIEWS.length} views: no horizontal overflow at ${WIDTH}px, ` +
      `0 console warnings/errors, 0 echoed secrets, ` +
      `reload/deep-link/back all stay where they should.`,
  );
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
