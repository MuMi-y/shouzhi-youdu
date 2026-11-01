'use strict';
// Offline startup check. Never contacts the model or prints credentials.
const fs=require('node:fs'),path=require('node:path');
const {createRequire}=require('node:module'),{pathToFileURL}=require('node:url');
const {loadModelRoute,authorizedProvider}=require('./model_route.cjs');
async function main(){
 const cli=process.env.DSH_PACKAGE_PATH?path.resolve(process.env.DSH_PACKAGE_PATH):path.resolve(__dirname,'../node-runtime/node_modules/@deepseek-ai/dsh');
 if(!fs.existsSync(path.join(cli,'package.json')))throw Error('项目依赖未安装：npm ci --prefix node-runtime --ignore-scripts --legacy-peer-deps');
 const req=createRequire(path.join(cli,'package.json'));
 if(req('./package.json').version!=='0.1.1-rc.2')throw Error('项目需要DSH 0.1.1-rc.2，请检查DSH_PACKAGE_PATH或按锁文件恢复依赖。');
 await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-app-boot')).href);
 const yaml=req('js-yaml');
 const home=process.env.DSH_CONFIG_HOME?path.resolve(process.env.DSH_CONFIG_HOME):path.join(process.env.USERPROFILE||require('node:os').homedir(),'.dsh');
 const file=process.env.DSH_CONFIG_HOME?path.join(home,'settings.yaml'):path.resolve(__dirname,'../config/settings.example.yaml');
 const policy=loadModelRoute();authorizedProvider(yaml.load(fs.readFileSync(file,'utf8')),policy);
 const credentials=path.join(home,'.credentials.yaml');
 const key=process.env[policy.apiKeyEnv]||(fs.existsSync(credentials)?yaml.load(fs.readFileSync(credentials,'utf8'))?.refs?.[policy.apiKeyEnv]:null);
 if(typeof key!=='string'||!key)throw Error('已指定模型的凭据不可用，请恢复ARK_AGENT_PLAN_API_KEY。');
 console.log('DSH project runtime: 0.1.1-rc.2; model configuration and credential reference available. No provider call made.');
}
main().catch(error=>{const text=String(error.message).replace(/(?:ark|sk)-[a-zA-Z0-9_-]{16,}/g,'[REDACTED]');console.error('AI启动检查未通过：'+text);process.exitCode=1;});
