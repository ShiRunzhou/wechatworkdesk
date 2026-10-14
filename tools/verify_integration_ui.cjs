// Run against integration_ui_fixture.py, with an isolated browser profile.
const {chromium}=require(process.env.WCWD_PLAYWRIGHT||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.WCWD_TEST_BROWSER||'msedge'});
 try {
 const context=await browser.newContext({httpCredentials:{username:'fixture',password:'fixture-password-only'}});
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:8880/update');await page.getByText('本机已连接',{exact:false}).waitFor();
 if(!await page.locator('#device-jobs').evaluate(n=>n.closest('.workflow-region')?.getAttribute('aria-labelledby')==='update-region-title'))throw Error('Update progress is outside update region');
 if(!await page.locator('#jobs').evaluate(n=>n.closest('.workflow-region')?.getAttribute('aria-labelledby')==='analysis-region-title'))throw Error('Analysis progress is outside analysis region');
 await page.locator('#update-only').click();await page.getByText('更新完成 · 已完成',{exact:false}).waitFor({timeout:20000});
 await page.locator('#analysis-summary').getByText('沿用更新范围',{exact:false}).waitFor();
 if(!await page.locator('#follow-update').isChecked())throw Error('Update range was not selected');
 await page.locator('#start-job').click();await page.getByText('查看 Agent 请求',{exact:true}).waitFor({timeout:20000});
 await page.getByText('查看 Agent 请求',{exact:true}).click();await page.locator('#agent-dialog').waitFor();await page.locator('#agent-close').click();
 await page.locator('#settings-open').click();await page.locator('#device-settings summary').click();await page.locator('[name=voice_mode]').selectOption('cache');await page.locator('#settings-form button[type=submit]').click();await page.getByText('设置已保存',{exact:true}).waitFor();
 await page.locator('#analysis-options summary').click();await page.locator('#analysis-range').selectOption('custom');await page.locator('#analysis-start').fill('2026-10-01');await page.locator('#analysis-end').fill('2026-10-02');if(await page.locator('#follow-update').isChecked())throw Error('Independent analysis did not detach');
 await page.goto('http://127.0.0.1:8880/');await page.locator('.module-nav').waitFor();
 const mobile=await context.newPage();await mobile.goto('http://127.0.0.1:8879/update');await mobile.getByText('本机未连接',{exact:false}).first().waitFor();if(!await mobile.locator('#update-only').isDisabled())throw Error('Website enabled device updates');if(await mobile.locator('input[type=file]').count())throw Error('Manual upload still visible');
 if(errors.length)throw Error(errors.join(';'));
 console.log('Browser verification passed: update, Agent request, settings, independent dates, event module, website view');
 } finally {await browser.close()}
})().catch(e=>{console.error(e.message);process.exit(1)});
