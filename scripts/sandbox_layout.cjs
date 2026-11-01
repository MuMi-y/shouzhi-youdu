'use strict';
// Shared file layout and fixed isolation policy for execution and offline preflight.
const fs=require('node:fs'),path=require('node:path');
const assetNames=['boot.cjs','driver.cjs','tools.cjs','persona.txt','moneydata_persona.txt'];
function prepareAssets(run,project=path.resolve(__dirname,'..')) {
  const assets=path.join(run,'assets');fs.mkdirSync(assets,{recursive:true});
  for(const name of assetNames)fs.copyFileSync(path.join(project,'plugins/bank-v1',name),path.join(assets,name));
  fs.copyFileSync(path.join(project,'scripts/dsh_container.cjs'),path.join(assets,'container.cjs'));
  return assets;
}
function containerArgs({name,cli,assets,requestFile,output,home,id,modules=path.resolve(__dirname,'../node-runtime/node_modules')}) {
  const args=['run','--interactive','--rm','--name',name,'--network','none','--read-only','--user','65534:65534',
    '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','768m','--cpus','1','--pids-limit','128',
    '--mount',`type=bind,source=${cli},target=/opt/dsh,readonly`,
    '--mount',`type=bind,source=${assets},target=/assets,readonly`,
    '--mount',`type=bind,source=${requestFile},target=/request.json,readonly`,
    '--mount',`type=bind,source=${output},target=/output`,
    '--mount',`type=bind,source=${home},target=/session`,
    '--tmpfs','/tmp:rw,noexec,nosuid,size=64m','-e','HOME=/tmp','-e','DSH_TELEMETRY_DISABLED=1',
    'node:22-slim','node','/assets/container.cjs',id];
  if(path.resolve(cli)===path.join(modules,'@deepseek-ai/dsh')) {
    args.splice(args.indexOf('--tmpfs'),0,'--mount',`type=bind,source=${modules},target=/opt/node_modules,readonly`);
  }
  return args;
}
module.exports={prepareAssets,containerArgs};
