'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {test,before,after}=require('node:test');
const {build}=require('esbuild');
const {chromium,expect}=require('@playwright/test');
let browser,server,base;
before(async()=>{
  const bundle=await build({entryPoints:[path.join(__dirname,'fixtures/public-output-layout.tsx')],bundle:true,write:false,
    outdir:'chat-theme-bundle',jsx:'automatic',format:'iife',define:{'process.env.NODE_ENV':'"production"'},
    plugins:process.env.AGENT_HUB_THEME_BASELINE?[{name:'legacy-theme-proof',setup(b){b.onLoad({filter:/conversation-workspace\.css$/},()=>({contents:'',loader:'css'}));}}]:[]});
  server=http.createServer((req,res)=>{
    if(req.url==='/brand/mofang-agent.jpg'){res.writeHead(200,{'Content-Type':'image/jpeg'});return res.end(fs.readFileSync(path.join(__dirname,'../public/brand/mofang-agent.jpg')));}
    const ext=req.url==='/fixture.css'?'.css':req.url==='/fixture.js'?'.js':null;
    res.writeHead(200,{'Content-Type':ext==='.css'?'text/css':ext==='.js'?'text/javascript':'text/html'});
    res.end(ext?bundle.outputFiles.find(f=>f.path.endsWith(ext)).text:'<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,"><link rel="stylesheet" href="/fixture.css"><body><div id="root"></div><script src="/fixture.js"></script></body></html>');
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));base='http://127.0.0.1:'+server.address().port;
  browser=await chromium.launch({channel:'msedge',headless:true,chromiumSandbox:true});
});
after(async()=>{if(browser)await browser.close();if(server)await new Promise(r=>server.close(r));});
for(const [name,viewport]of[['mobile',{width:390,height:844}],['compact-desktop',{width:980,height:800}],['desktop',{width:1280,height:900}],['wide',{width:1600,height:1000}]]){
  test(name+': light workspace keeps conversation and composer readable',async()=>{
    const context=await browser.newContext({viewport});const page=await context.newPage();const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    try{
      await page.goto(base+'/shell');
      await expect(page.getByRole('region',{name:'主对话内容',exact:true})).toBeVisible();
      assert.equal(await page.locator('.page-surface').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      assert(await page.locator('.chat-message.user').evaluate(el=>el.scrollHeight<=el.clientHeight+1),'user bubble clipped');
      const composer=page.getByRole('form',{name:'发送消息',exact:true}),input=composer.locator('textarea').first();
      const box=await composer.boundingBox();assert(box.y>=0&&box.y+box.height<=viewport.height-4,JSON.stringify(box));
      const stream=await page.locator('.chat-stream').boundingBox();assert(stream.y+stream.height<=box.y+2);
      assert.equal(await input.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
      if(viewport.width>960){assert((await page.locator('.sidebar').boundingBox()).width<=220);assert(stream.width>=700);}
      await expect(page.getByText('连续对话窗口，可承接多轮任务、上下文压缩和历史会话。',{exact:true})).toHaveCount(0);
      await page.getByRole('button',{name:'打开本次运行配置',exact:true}).click();
      const settings=page.getByRole('dialog',{name:'本次运行设置',exact:true});await expect(settings).toBeVisible();
      assert.equal(await settings.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
      await settings.getByRole('button',{name:'关闭运行设置',exact:true}).click();
      await page.getByRole('button',{name:'打开项目与会话',exact:true}).click();
      await expect(page.getByRole('navigation',{name:'项目与会话管理',exact:true})).toBeVisible();
      await page.locator('.conversation-list-header').getByRole('button',{name:'关闭项目与会话',exact:true}).click();
      if(viewport.width<640){await page.setViewportSize({width:viewport.width,height:520});const small=await composer.boundingBox();assert(small.y+small.height<=516);}
      await page.setViewportSize(viewport);
      await page.locator('.chat-stream').evaluate(el=>{el.scrollTop=0;});
      if(process.env.AGENT_HUB_LAYOUT_EVIDENCE_DIR){fs.mkdirSync(process.env.AGENT_HUB_LAYOUT_EVIDENCE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.AGENT_HUB_LAYOUT_EVIDENCE_DIR,name+'-conversation-light.png')});}
      assert.deepEqual(errors,[]);
    }finally{await context.close();}
  });
}
