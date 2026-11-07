"""Render documentation images from the current UI and synthetic data.

Run from the repository: python packaging/capture_docs.py
Requires Playwright and an installed browser (the existing login component or
Chrome). All requests are intercepted; no local service or real account is used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import expect, sync_playwright
from codex_switcher import __version__
from codex_switcher.components import browser_launch_options

# A fixed clock keeps dates, relative ages and chart labels reproducible.
NOW = datetime(2026, 11, 8, 8, tzinfo=timezone.utc)
EPOCH = int(NOW.timestamp())


def ago(minutes=0):
    return (NOW - timedelta(minutes=minutes)).isoformat()


def fixtures():
    profiles = []
    for key, email, name, workspace, plan, current, priority in (
        ('personal', 'lin@example.com', '林晨', '个人空间', 'plus', True, 100),
        ('team', 'lin@example.com', '林晨', '产品设计团队', 'team', False, 90),
        ('pro', 'zhou@example.com', '周文', '个人空间', 'pro', False, 80),
    ):
        profiles.append(dict(profile_id='profile_demo_' + key, email=email, email_masked=email,
            name=name, workspace_id='workspace_demo_' + key, account_id='workspace_demo_' + key,
            user_id='user_demo_' + email.split('@')[0], workspace_name=workspace,
            identity_type='team' if plan == 'team' else 'personal', plan=plan,
            is_current=current, expired=False, saved_at=ago(40), expires_at=EPOCH + 7 * 86400,
            priority=priority, disabled=False, in_pool=True))
    accounts = [dict(email=email, email_masked=email, name=name, status=status, priority=priority,
                     has_password=True, has_totp=totp, totp_format='base32' if totp else None,
                     has_auth_snapshot=True, disabled=False,
                     profiles=[p for p in profiles if p['email'] == email])
                for email, name, status, priority, totp in (
                    ('lin@example.com', '林晨', 'active', 100, True),
                    ('zhou@example.com', '周文', 'ready', 80, False))]
    for account in accounts:
        account['profile_count'] = len(account['profiles'])
    auth = dict(profiles[0], logged_in=True)
    switching = dict(implemented=True, enabled=True, message='', running=False)
    auto = dict(enabled=True, running=True, last_result='quota_ok', last_check_at=ago(),
                switches_last_hour=0, max_switches_per_hour=3, predictive_enabled=False,
                interval_seconds=90, cooldown_seconds=900, failure_threshold=3,
                auto_reset=dict(enabled=False, gate='disabled', secondary_threshold=98,
                                min_wait_hours=72, max_per_week=1, used_last_7d=0), events=[])

    def quota(profile, five, seven, credits):
        return dict(ok=True, source='live', email=profile['email'], profile_id=profile['profile_id'],
                    workspace_id=profile['workspace_id'], account_id=profile['account_id'],
                    user_id=profile['user_id'], plan=profile['plan'], allowed=True,
                    limit_reached=False, pending=False, stale=False, fetched_at=ago(),
                    primary_window=dict(used_percent=five, reset_at=EPOCH + 4 * 3600, window_seconds=18000),
                    secondary_window=dict(used_percent=seven, reset_at=EPOCH + 5 * 86400, window_seconds=604800),
                    reset_credits=dict(known=True, available_count=credits, applicable_available_count=0))
    quotas = {profile['profile_id']: quota(profile, five, seven, credits)
              for profile, five, seven, credits in zip(profiles, (35, 12, 58), (62, 24, 41), (2, 0, 1))}
    threads = [dict(thread_id='10000001-1111-4111-8111-111111111111', title='完善网站并通过验收',
                    cwd='C:/Projects/demo-website', project_name='demo-website', has_goal=True,
                    goal_status='active', goal_objective='完成网站功能、修复问题并通过验收测试',
                    continuable=True, pinned=True, updated_at=ago(1),
                    execution=dict(state='running', turn_id='demo-turn-01', trigger='queue', at=ago(3))),
               dict(thread_id='10000002-1111-4111-8111-111111111111', title='补充导入流程测试',
                    cwd='C:/Projects/demo-app', project_name='demo-app', has_goal=True,
                    goal_status='paused', goal_objective='补充账号导入流程的回归测试',
                    continuable=True, updated_at=ago(2)),
               dict(thread_id='10000003-1111-4111-8111-111111111111', title='整理发布说明',
                    cwd='C:/Projects/demo-app', project_name='demo-app', has_goal=True,
                    goal_status='paused', goal_objective='整理变更并核对文档',
                    continuable=True, updated_at=ago(5))]
    receipts = {
        threads[0]['thread_id']: dict(ok=True, owned_queue=True, delivered=True, requested_at=ago(4),
            queued_at=ago(4), queued_id='demo-queue-01', execution=dict(state='running',
            turn_id='demo-turn-01', started_at=ago(3))),
        threads[1]['thread_id']: dict(ok=True, owned_queue=True, pending=True, requested_at=ago(2),
            queued_at=ago(2), queued_id='demo-queue-02', execution=dict(state='queued')),
        threads[2]['thread_id']: dict(ok=True, owned_queue=True, delivered=True, requested_at=ago(10),
            queued_at=ago(10), queued_id='demo-queue-03', execution=dict(state='completed',
            turn_id='demo-turn-03', started_at=ago(9), completed_at=ago(5))),
    }
    rows = []
    for thread, group, state, label, source, once in zip(threads,
        ('running', 'waiting', 'recent'), ('running', 'queued', 'paused'),
        ('正在续跑', '已排队', '已暂停'), ('automatic', 'manual', 'manual'), (False, True, False)):
        receipt = receipts[thread['thread_id']]
        rows.append(dict(thread_id=thread['thread_id'], title=thread['title'], cwd=thread['cwd'],
            project_name=thread['project_name'], group=group, state=state, label=label, source=source,
            continue_once=once, requested_at=receipt['requested_at'],
            started_at=receipt['execution'].get('started_at'), objective=thread['goal_objective'],
            goal_status=thread['goal_status'], can_cancel=state == 'queued', can_pause=group != 'recent',
            can_resume=group == 'recent', can_remove=True, result=receipt, attempts=[dict(receipt,
                started_via='switch:lin@example.com' if source == 'automatic' else 'manual')]))
    goals = dict(ok=True, session_api_version=5, enabled=True, running=False, job=None, history=[],
        goals=[dict(thread_id=t['thread_id'], objective=t['goal_objective'], status=t['goal_status'], armable=True)
               for t in threads],
        sessions=dict(total=3, shown=3, truncated=False, counts=dict(goals=3),
                      pinned=[threads[0]], recent=threads[1:], projects=[]),
        auto_resume=dict(enabled=True, thread_ids=[threads[0]['thread_id']]),
        last_resume=receipts, pending_resume={threads[1]['thread_id']: receipts[threads[1]['thread_id']]},
        execution={threads[0]['thread_id']: threads[0]['execution']},
        resume_queue=dict(items=rows, counts=dict(running=1, waiting=1, attention=0, recent=1), active=2, parallel=True),
        queue_watch=dict(pending=1), execution_watch=dict(active=1))
    return {
        '/api/theme': dict(ok=True, theme='dark'),
        '/api/status': dict(ok=True, version=__version__, auth=auth, switching=switching,
                            accounts=dict(total=2, by_status=dict(active=1, ready=1)), auto_switch=auto),
        '/api/accounts': dict(ok=True, accounts=accounts, login_concurrency=2),
        '/api/quota': dict(ok=True, quota=quotas[profiles[0]['profile_id']], pending=False),
        '/api/accounts/quotas': dict(ok=True, quotas=quotas, pending=[], skipped=[]),
        '/api/snapshots': dict(ok=True, current=auth, switching=switching, snapshots=profiles),
        '/api/auto-switch/status': dict(ok=True, auto_switch=auto),
        '/api/quota/history': dict(ok=True, samples=[dict(epoch=EPOCH - (20 - i) * 300,
            primary_used=round(8 + i * 1.35, 1), secondary_used=round(51 + i * .55, 1)) for i in range(21)],
            prediction=dict(primary=dict(available=True, rate_per_hour=16),
                            soonest_window='primary', soonest_eta_seconds=4 * 3600)),
        '/api/goals': goals,
        '/api/login/status': dict(ok=True, login_enabled=True, sessions=[], queue=[], history=[]),
        '/api/setup/status': dict(ok=True, show_guide=False, accounts=dict(total=2, ready=2),
                                 local_account=dict(available=False)),
        '/api/sms/status': dict(ok=True, enabled=False, activations=[], events=[]),
        '/api/desktop/status': dict(ok=True, desktop=dict(running=True, enabled=True, available=True, pids=[1234]),
                                   pending=False, fetched_at=ago()),
        '/api/extension/status': dict(ok=True, installed=True, extension=dict(present=True,
            version=__version__, path='C:/Projects/codex-switcher/extension')),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    data = fixtures()
    mutations, unexpected, errors = [], [], []
    manifest = json.loads((ROOT / 'extension/manifest.json').read_text(encoding='utf-8'))

    def intercept(route):
        request = route.request
        parsed = urlparse(request.url)
        if request.method != 'GET':
            mutations.append(request.url)
            route.abort()
        elif parsed.path in data:
            route.fulfill(status=200, content_type='application/json', body=json.dumps(data[parsed.path]))
        elif parsed.path == '/api/theme/events':
            route.fulfill(status=200, content_type='text/event-stream', body='data: {"theme":"dark"}\n\n')
        elif parsed.path in ('/', '/extension/sidepanel.html', '/extension/sidepanel.js'):
            file = ROOT / ('codex_switcher/dashboard.html' if parsed.path == '/' else parsed.path.lstrip('/'))
            route.fulfill(status=200, content_type='application/javascript' if parsed.path.endswith('.js') else 'text/html',
                          body=file.read_text(encoding='utf-8'))
        else:
            unexpected.append(request.url)
            route.abort()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, **browser_launch_options())
        try:
            context = browser.new_context(viewport=dict(width=1440, height=1100), device_scale_factor=1.5,
                locale='zh-CN', timezone_id='Asia/Shanghai', color_scheme='dark', service_workers='block')
            context.route('**/*', intercept)
            context.add_init_script('''(() => {
                const NativeDate = Date, fixed = ''' + str(EPOCH * 1000) + ''';
                window.Date = class extends NativeDate {
                    constructor(...args) { super(...(args.length ? args : [fixed])); }
                    static now() { return fixed; }
                };
                localStorage.setItem('codex-sidepanel-theme', 'dark');
                window.chrome = {
                    runtime: {getManifest: () => ({version: ''' + json.dumps(manifest['version']) + '''}),
                              sendMessage: (_message, callback) => callback && callback({ok:true})},
                    storage: {local: {get: async defaults => defaults}},
                };
            })();''')
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://docs-preview.test/', wait_until='domcontentloaded')
            expect(page.locator('#version')).to_have_text('v' + __version__)
            expect(page.locator('#statAccounts')).to_have_text('2')
            expect(page.locator('#accountsWrap .account-row')).to_have_count(2)
            expect(page.locator('#accountsWrap .workspace-entry')).to_have_count(2)
            expect(page.locator('#accountsWrap')).to_contain_text('产品设计团队')
            expect(page.locator('#accountsWrap')).to_contain_text('剩余 0 张券')
            expect(page.locator('#accountsWrap .workspace-entry').nth(1)
                   .get_by_role('button', name='用券', exact=True)).to_be_disabled()
            expect(page.locator('#authBadge')).to_have_text('已登录')
            expect(page.locator('#resumeQueueCount')).to_contain_text('续跑中 1 · 等待 1')
            expect(page.locator('#resumeQueue')).to_contain_text('持续续跑')
            expect(page.locator('#resumeQueue')).to_contain_text('单次续跑')
            page.locator('#trendChart path.line-5h').wait_for(state='attached')
            page.wait_for_function('Array.from(document.images).every(image => image.complete)')
            # Capture natural components without rearranging the page. Enough
            # viewport padding keeps the floating navigation below each crop.
            for selector, filename in (('#accounts', 'dashboard.png'),
                                       ('#resumeQueue', 'resume-queue.png')):
                component = page.locator(selector)
                box = component.bounding_box()
                page.set_viewport_size(dict(width=1440, height=max(2000, math.ceil(box['height'] + 400))))
                component.evaluate('''(element) => window.scrollTo({
                    top: element.getBoundingClientRect().top + window.scrollY - 80,
                    behavior: 'instant'
                })''')
                component.screenshot(path=str(output / filename), animations='disabled')
            page.close()

            panel = context.new_page()
            panel.set_viewport_size(dict(width=420, height=600))
            panel.on('pageerror', lambda error: errors.append(str(error)))
            panel.goto('http://docs-preview.test/extension/sidepanel.html', wait_until='domcontentloaded')
            expect(panel.locator('#ver')).to_have_text('v' + manifest['version'])
            expect(panel.locator('#account')).to_contain_text('林晨')
            expect(panel.locator('#quotaHint')).to_contain_text('重置券 2 张')
            expect(panel.locator('#dot')).to_have_class('dot online')
            # Opening a local disclosure is read-only and shows workspace targets.
            panel.locator('#switchToggle').click()
            expect(panel.locator('#switchBox')).to_be_visible()
            expect(panel.locator('#switchTarget')).to_contain_text('产品设计团队')
            expect(panel.locator('#switchBadge')).to_have_text('可切换账号')
            panel.screenshot(path=str(output / 'sidepanel.png'), animations='disabled')
            context.close()
        finally:
            browser.close()
    assert not mutations, mutations
    assert not unexpected, unexpected
    assert not errors, errors
    print(json.dumps(dict(version=__version__, example_data_only=True, mutations=mutations,
        unexpected_requests=unexpected, browser_errors=errors,
        images=[str(output / name) for name in ('dashboard.png', 'resume-queue.png', 'sidepanel.png')]),
        ensure_ascii=False))


if __name__ == '__main__':
    main()
