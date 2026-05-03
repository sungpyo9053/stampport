#!/usr/bin/env node
// Real-render verification + screenshot capture.
//
// Spins up `vite preview` against the latest build, opens it in
// headless Chromium at desktop (1440x900) and mobile (390x844)
// viewports, asserts the pixel-office DOM is actually present in
// the rendered tree, captures screenshots, and exits non-zero on
// any failure.
//
// Output:
//   .runtime/ui-agent-office-desktop.png
//   .runtime/ui-agent-office-mobile.png
//   .runtime/ui_agent_office_render_assertions.json

import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const REPO_ROOT = resolve(HERE, "..", "..", "..");
const RUNTIME_DIR = resolve(REPO_ROOT, ".runtime");

// Spawn the vite preview server. We let Vite pick a port and then
// parse the chosen URL out of stdout — port 4173 is the default but
// it might already be in use on the operator's box.
function startPreview() {
  return new Promise((resolveP, rejectP) => {
    const proc = spawn(
      "npx",
      ["vite", "preview", "--port", "4173", "--strictPort", "false", "--host", "127.0.0.1"],
      { cwd: ROOT, stdio: ["ignore", "pipe", "pipe"] },
    );
    let url = null;
    let buf = "";
    const onData = (chunk) => {
      buf += chunk.toString();
      const m = buf.match(/Local:\s+(http:\/\/[^\s]+)/);
      if (m && !url) {
        // Vite logs the URL without a trailing slash but its base
        // path needs one — without it the preview server returns a
        // "did you mean /stampport-control/" hint instead of index.html.
        const raw = m[1];
        url = raw.endsWith("/") ? raw : raw + "/";
        resolveP({ proc, url });
      }
    };
    proc.stdout.on("data", onData);
    proc.stderr.on("data", onData);
    proc.on("error", rejectP);
    setTimeout(() => {
      if (!url) rejectP(new Error("vite preview did not log a URL within 15s"));
    }, 15000);
  });
}

async function captureViewport(browser, baseUrl, viewport, label, outPath) {
  const ctx = await browser.newContext({
    viewport,
    deviceScaleFactor: 2,
  });
  const page = await ctx.newPage();
  // Give the page a chance to mount + fail soft on the API 404 (we're
  // running against the static preview, not the live FastAPI).
  page.on("pageerror", (err) => {
    console.warn(`[capture] ${label} page error:`, err.message);
  });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  // Wait for the office scene to mount.
  await page.waitForSelector('[data-testid="pixel-office-scene"]', { timeout: 8000 });
  // Let one polling tick fail and the demo bubbles render.
  await page.waitForTimeout(1500);

  const assertions = await page.evaluate(() => {
    const $ = (sel) => document.querySelectorAll(sel).length;
    const text = document.body.innerText || "";
    // No-overflow guard: every agent slot must fit inside the
    // viewport's clientWidth. The scene as a whole must not
    // produce a horizontal scrollbar.
    const docWidth = document.documentElement.clientWidth;
    const docScrollW = document.documentElement.scrollWidth;
    const horizontalOverflow = docScrollW - docWidth; // >0 means overflow
    return {
      pixel_office_scene: $('.pixel-office-scene'),
      agent_office_zone: $('.agent-office-zone'),
      agent_office_zone_plan:  $('.agent-office-zone-plan'),
      agent_office_zone_build: $('.agent-office-zone-build'),
      agent_office_zone_ship:  $('.agent-office-zone-ship'),
      agent_slot: $('.agent-slot'),
      agent_speech_bubble: $('.agent-speech-bubble'),
      pixel_agent: $('.pixel-agent'),
      pixel_agent_head: $('.pixel-agent-head'),
      pixel_agent_body: $('.pixel-agent-body'),
      pixel_agent_arm: $('.pixel-agent-arm'),
      pixel_agent_leg: $('.pixel-agent-leg'),
      pixel_agent_nameplate: $('.pixel-agent-nameplate'),
      agent_character: $('.agent-character'),
      agent_character_face: $('.agent-character-face'),
      agent_character_desk: $('.agent-character-desk'),
      autopilot_hero: $('[data-testid="autopilot-hero"]'),
      autopilot_payload_preview: $('[data-testid="autopilot-payload-preview"]'),
      autopilot_restart_button: $('[data-testid="autopilot-restart"]'),
      office_headline: $('[data-testid="office-headline"]'),
      hero_text_present: text.includes("AUTO PILOT"),
      office_text_present: text.includes("AGENT OFFICE"),
      plan_zone_label: text.includes("PLAN ZONE"),
      build_zone_label: text.includes("BUILD ZONE"),
      ship_zone_label: text.includes("SHIP ZONE"),
      doc_client_width: docWidth,
      doc_scroll_width: docScrollW,
      horizontal_overflow: horizontalOverflow,
    };
  });

  await page.screenshot({ path: outPath, fullPage: true });

  // Click an agent and verify the drawer opens. Only do this on the
  // desktop pass — the mobile screenshot stays at the office scene.
  let drawerSummary = null;
  if (label === "desktop") {
    const target = await page.$('[data-testid="pixel-agent-pm"]');
    if (target) {
      await target.click();
      await page.waitForSelector('[data-testid="agent-detail-drawer"]', { timeout: 4000 });
      drawerSummary = await page.evaluate(() => {
        const text = document.body.innerText || "";
        return {
          drawer_visible: document.querySelectorAll('[data-testid="agent-detail-drawer"]').length > 0,
          현재_역할: text.includes("현재 역할"),
          현재_작업: text.includes("현재 작업"),
          마지막_명령: text.includes("마지막 명령"),
          최근_로그: text.includes("최근 로그"),
          실패_원인: text.includes("실패 원인"),
          다음_액션: text.includes("다음 액션"),
          관련_파일_변경: text.includes("관련 파일 변경"),
        };
      });
      // Capture the drawer-open state too — useful evidence.
      await page.screenshot({
        path: outPath.replace(".png", "-drawer.png"),
        fullPage: false,
      });
      // Close via Escape so subsequent runs aren't affected.
      await page.keyboard.press("Escape");
    }
  }

  await ctx.close();
  return { viewport, label, assertions, drawerSummary, screenshot: outPath };
}

async function main() {
  const { proc, url } = await startPreview();
  console.log(`[capture] preview running at ${url}`);

  // Tear down on any error.
  let browser = null;
  let exitCode = 0;
  try {
    browser = await chromium.launch();

    const desktop = await captureViewport(
      browser,
      url,
      { width: 1440, height: 900 },
      "desktop",
      join(RUNTIME_DIR, "ui-agent-office-desktop.png"),
    );

    const mobile = await captureViewport(
      browser,
      url,
      { width: 390, height: 844 },
      "mobile",
      join(RUNTIME_DIR, "ui-agent-office-mobile.png"),
    );

    // Acceptance — every count must be > 0, drawer sections must all
    // be present, no horizontal overflow, ≤3 bubbles, 3 zones each.
    const requirePositive = [
      "pixel_office_scene",
      "agent_office_zone",
      "agent_slot",
      "pixel_agent",
      "pixel_agent_head",
      "pixel_agent_body",
      "pixel_agent_arm",
      "pixel_agent_leg",
      "agent_character",
      "agent_character_face",
      "agent_character_desk",
      "office_headline",
    ];
    const failures = [];
    for (const [k, v] of Object.entries(desktop.assertions)) {
      if (requirePositive.includes(k) && !(v > 0)) {
        failures.push(`desktop.${k} === ${v}`);
      }
    }
    for (const view of [desktop, mobile]) {
      const a = view.assertions;
      // Zones present
      if (a.agent_office_zone_plan === 0)  failures.push(`${view.label} PLAN zone missing`);
      if (a.agent_office_zone_build === 0) failures.push(`${view.label} BUILD zone missing`);
      if (a.agent_office_zone_ship === 0)  failures.push(`${view.label} SHIP zone missing`);
      if (!a.plan_zone_label)  failures.push(`${view.label} PLAN ZONE label text missing`);
      if (!a.build_zone_label) failures.push(`${view.label} BUILD ZONE label text missing`);
      if (!a.ship_zone_label)  failures.push(`${view.label} SHIP ZONE label text missing`);
      // 8 agents
      if (!(a.pixel_agent >= 8)) {
        failures.push(`${view.label} pixel_agent count was ${a.pixel_agent} (<8)`);
      }
      if (!(a.agent_slot >= 8)) {
        failures.push(`${view.label} agent_slot count was ${a.agent_slot} (<8)`);
      }
      // Bubble hard cap
      if (a.agent_speech_bubble > 3) {
        failures.push(`${view.label} agent_speech_bubble count was ${a.agent_speech_bubble} (>3)`);
      }
      // No horizontal overflow
      if (a.horizontal_overflow > 1) {
        failures.push(
          `${view.label} horizontal overflow ${a.horizontal_overflow}px (` +
          `scrollWidth=${a.doc_scroll_width} clientWidth=${a.doc_client_width})`,
        );
      }
    }
    if (!desktop.assertions.hero_text_present) failures.push("desktop hero text missing");
    if (!desktop.assertions.office_text_present) failures.push("desktop office text missing");
    if (!desktop.drawerSummary) {
      failures.push("desktop drawer did not open after clicking pixel-agent-pm");
    } else {
      const sections = [
        "현재_역할", "현재_작업", "마지막_명령", "최근_로그",
        "실패_원인", "다음_액션", "관련_파일_변경",
      ];
      for (const s of sections) {
        if (!desktop.drawerSummary[s]) failures.push(`drawer missing ${s}`);
      }
    }

    const summary = {
      ok: failures.length === 0,
      preview_url: url,
      desktop,
      mobile,
      failures,
      generated_at: new Date().toISOString(),
    };
    writeFileSync(
      join(RUNTIME_DIR, "ui_agent_office_render_assertions.json"),
      JSON.stringify(summary, null, 2),
      "utf8",
    );

    if (failures.length === 0) {
      console.log(`[capture] PASS — ${desktop.assertions.pixel_agent} desktop agents, ${mobile.assertions.pixel_agent} mobile agents`);
    } else {
      console.error("[capture] FAILED:");
      for (const f of failures) console.error(`  · ${f}`);
      exitCode = 1;
    }
  } catch (e) {
    console.error("[capture] threw:", e);
    exitCode = 1;
  } finally {
    if (browser) await browser.close();
    proc.kill();
  }
  process.exit(exitCode);
}

main();
