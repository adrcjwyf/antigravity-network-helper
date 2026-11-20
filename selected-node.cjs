const fs = require("fs");
const http = require("http");
const prefix = "\\\\.\\pipe\\";
async function main() {
  const file = require('path').join(process.env.APPDATA, 'io.github.clash-verge-rev.clash-verge-rev', 'clash-verge.yaml');
  const match = fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '').match(/^external-controller-pipe:[ \t]*(.+)$/m);
  if (!match) throw Error('external-controller-pipe is missing from clash-verge.yaml');
  let pipe = match[1].replace(/\s+#.*$/, '').trim();
  if (pipe.startsWith('"')) pipe = JSON.parse(pipe);
  else if (pipe.startsWith("'") && pipe.endsWith("'")) pipe = pipe.slice(1,-1).replace(/''/g,"'");
  if (!pipe.startsWith(prefix) || !/^[\w.-]+$/.test(pipe.slice(prefix.length))) throw Error('Expected a local Windows controller pipe.');
  function get(path) {
    return new Promise((resolve,reject) => {
      const req=http.get({socketPath:pipe,path}, res=>{
        let body=""; res.on("data",c=>body+=c);
        res.on("end",()=>{try{if(res.statusCode!==200)throw Error("Clash controller unavailable");resolve(JSON.parse(body))}catch(e){reject(e)}});
      });
      req.setTimeout(6000,()=>req.destroy(Error("Clash timeout")));req.on("error",reject);
    });
  }
  let cfg;
  try { cfg = await get('/configs'); }
  catch (err) {
    const id = pipe.match(/-([a-f0-9]{64})$/i)?.[1];
    if (err.code !== 'ENOENT' || !id) throw err;
    const candidates = fs.readdirSync(prefix).filter(n => n.startsWith('verge-mihomo-') && n.endsWith('-'+id));
    if (candidates.length !== 1) throw Error('Configured controller is unavailable; no unique pipe for this instance.');
    pipe = prefix + candidates[0];
    cfg = await get('/configs');
  }
  const {proxies} = await get('/proxies');
  if(cfg.tun?.enable) throw Error("Use system proxy mode with TUN off.");
  let name="GLOBAL";
  if(cfg.mode==="rule"){
    const {rules}=await get("/rules");
    const last=rules.at(-1);
    if(last?.type?.toLowerCase()!=="match")throw Error("No identifiable default rule group.");
    name=last.proxy;
  } else if(cfg.mode!=="global") throw Error("Select a proxy in Clash first.");
  const seen=new Set();
  while(proxies[name]?.now){if(seen.has(name))throw Error("Proxy group loop");seen.add(name);name=proxies[name].now;}
  if(!proxies[name]||["direct","reject","pass","relay"].includes(proxies[name].type.toLowerCase()))throw Error("Select a single proxy node.");
  process.stdout.write(JSON.stringify({node:name}));
}
main().catch(e=>{console.error(e.message);process.exitCode=1});
