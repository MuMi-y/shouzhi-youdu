'use strict';
// DSH's public app-boot API, without the CLI's Windows dependency-junction healer.
const fs=require('node:fs'),path=require('node:path');
const {createRequire}=require('node:module');
const {pathToFileURL}=require('node:url');
async function main() {
  const cli=process.env.BANK_V1_DSH_PACKAGE;
  const req=createRequire(path.join(cli,'package.json'));
  const load=async name=>import(pathToFileURL(req.resolve(name)).href);
  const {boot,loadOverlayPatches,loadLayeredEnv,installFailLoud}=await load('@deepseek-ai/dsh-app-boot');
  const {DSH_LAUNCH_ENVIRONMENT_KEY}=await load('@deepseek-ai/dsh-launch-environment');
  const profile=path.join(process.env.DSH_HOME,'profiles/bank-v1');
  const root=path.join(profile,'cordis.yml');fs.writeFileSync(root,'[]\n');
  const patches=[...loadOverlayPatches('bank-v1',req.resolve('@deepseek-ai/dsh-base/cordis.patch.yml')),...loadOverlayPatches('bank-v1',path.join(profile,'cordis.patch.yml'))];
  // Resolve module names in trusted deployment configuration, never in model data.
  const resolveRows=rows=>{for(const row of rows){if(row.name&&!row.name.startsWith('file:'))row.name=pathToFileURL(req.resolve(row.name)).href;if(Array.isArray(row.insert))resolveRows(row.insert);if(Array.isArray(row.config))resolveRows(row.config);}};
  resolveRows(patches);
  let app,closing=false;
  const shutdown=async code=>{
    if(closing)return;closing=true;
    const force=setTimeout(()=>process.exit(code),4000);
    try{await app?.fiber.dispose();}finally{clearTimeout(force);process.exitCode=code;}
  };
  installFailLoud('bank-v1',process,async()=>{await app?.fiber.dispose();});
  process.once('SIGINT',()=>shutdown(130));process.once('SIGTERM',()=>shutdown(143));
  await boot('bank-v1',root,patches,ctx=>{
    app=ctx;
    ctx.provide(DSH_LAUNCH_ENVIRONMENT_KEY,loadLayeredEnv('bank-v1',process.cwd()));
    ctx.provide('appExit',code=>void shutdown(code));
  },pathToFileURL(cli+path.sep).href);
}
main().catch(error=>{process.stderr.write(String(error.stack||error));process.exitCode=1;});
