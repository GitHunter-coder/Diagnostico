/* Modo navegador: medición ampliada, hallazgos, dashboard y Excel con el mismo estilo del informe del programa local.
   Nada de esto lee SMART (ningún navegador puede): esas partes se marcan N/D con su motivo. */
"use strict";

const UMBRALES_NAV = { escritura_obs: 100, escritura_crit: 20, iops_obs: 5000, iops_crit: 500, p99_obs: 10, p99_crit: 50, caida_obs: 30, caida_crit: 60, uso_obs: 90 };
const VERSION_INFORME_NAV = "V3-nav";

/* ---------------- Ayudas contextuales (globos) ---------------- */
const TIPS_CSS = `
.tip{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;margin-left:6px;border:1px solid #64748b;border-radius:50%;font:600 10px/1 system-ui,sans-serif;color:#94a3b8;cursor:help;vertical-align:middle;user-select:none;flex:none}
.tip:hover,.tip:focus-visible{color:#fff;border-color:#93c5fd;outline:none}
[data-tip]:not(.tip){cursor:help}
#tipBox{position:fixed;z-index:99999;max-width:280px;padding:9px 12px;border-radius:8px;background:#020617;color:#e2e8f0;border:1px solid #475569;box-shadow:0 8px 24px rgba(0,0,0,.45);font:400 12px/1.45 system-ui,sans-serif;text-transform:none;letter-spacing:0;text-align:left;pointer-events:none;opacity:0;transform:translateY(4px);transition:opacity .12s,transform .12s;left:0;top:0}
#tipBox.v{opacity:1;transform:none}
@media (prefers-reduced-motion:reduce){#tipBox{transition:none}}`;
function iniciarTips() {
  const caja = document.createElement("div"); caja.id = "tipBox"; caja.setAttribute("role", "tooltip"); document.body.appendChild(caja);
  let actual = null, t0 = 0;
  const ocultar = () => { caja.classList.remove("v"); if (actual) actual.removeAttribute("aria-describedby"); actual = null; };
  const mostrar = (el) => {
    const t = el.getAttribute("data-tip"); if (!t) return;
    caja.textContent = t; caja.classList.add("v"); actual = el; t0 = Date.now(); el.setAttribute("aria-describedby", "tipBox");
    const r = el.getBoundingClientRect(), w = caja.offsetWidth, h = caja.offsetHeight, m = 8;
    const x = Math.min(Math.max(m, r.left + r.width / 2 - w / 2), Math.max(m, window.innerWidth - w - m));
    let y = r.top - h - 10; if (y < m) y = r.bottom + 10;
    caja.style.left = x + "px"; caja.style.top = y + "px";
  };
  const buscar = (e) => (e.target && e.target.closest) ? e.target.closest("[data-tip]") : null;
  const tactil = () => window.matchMedia && matchMedia("(hover: none)").matches;
  document.addEventListener("mouseover", (e) => { if (tactil()) return; const el = buscar(e); if (el && el !== actual) mostrar(el); });
  document.addEventListener("mouseout", (e) => { if (tactil()) return; const el = buscar(e); if (el && !(e.relatedTarget && el.contains(e.relatedTarget))) ocultar(); });
  document.addEventListener("focusin", (e) => { const el = buscar(e); if (el) mostrar(el); });
  document.addEventListener("focusout", (e) => { if (buscar(e)) ocultar(); });
  document.addEventListener("click", (e) => {
    const el = buscar(e);
    if (el && el.classList.contains("tip")) e.preventDefault();            // el ⓘ nunca activa la casilla/enlace que lo contiene
    if (el && tactil()) { if (actual !== el) mostrar(el); else if (Date.now() - t0 > 400) ocultar(); }   // el toque táctil también dispara mouseover: se ignora el rebote else if (!el) ocultar();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") ocultar(); });
  window.addEventListener("scroll", ocultar, true);
}

/* ---------------- Medición (Web Worker sobre OPFS) ---------------- */
const WORKER_NAV = `
self.onmessage = async (ev) => {
  const { mb, sost } = ev.data; let root, h;
  const pct = (a, p) => a[Math.min(a.length - 1, Math.floor(a.length * p))];
  try {
    root = await navigator.storage.getDirectory();
    const fh = await root.getFileHandle('bench.tmp', { create: true });
    h = await fh.createSyncAccessHandle();
    const B = 4 * 1024 * 1024, buf = new Uint8Array(B);
    for (let i = 0; i < B; i += 65536) crypto.getRandomValues(buf.subarray(i, i + 65536));
    const n = Math.max(1, Math.ceil(mb / 4)), total = n * 4;
    const w = sost ? { s: 0.2, r: 0.1, a: 0.1 } : { s: 0.5, r: 0.3, a: 0.2 };   // peso de cada fase en la barra
    const linea = []; let base = 0;
    // 1) escritura secuencial con línea de tiempo
    let t0 = performance.now(), tm = t0, bm = 0;
    for (let i = 0; i < n; i++) {
      h.write(buf, { at: i * B }); bm += B; const now = performance.now();
      if (now - tm >= 400 || i === n - 1) { linea.push({ t: (now - t0) / 1000, mbs: bm / 1048576 / Math.max((now - tm) / 1000, 1e-6), fase: 'Escritura' }); tm = now; bm = 0; }
      if (i % 8 === 0) postMessage({ p: base + w.s * (i + 1) / n, f: 'Escribiendo (secuencial)' });
    }
    h.flush(); const tW = (performance.now() - t0) / 1000; base += w.s;
    // 2) lectura secuencial
    const rb = new Uint8Array(B); t0 = performance.now();
    for (let i = 0; i < n; i++) { h.read(rb, { at: i * B }); if (i % 8 === 0) postMessage({ p: base + w.r * (i + 1) / n, f: 'Leyendo (secuencial)' }); }
    const tR = (performance.now() - t0) / 1000; base += w.r;
    // 3) 4K aleatorio: escritura y lectura, cada operación cronometrada
    const iter = 3000, b4 = new Uint8Array(4096); crypto.getRandomValues(b4); const slots = Math.max(1, Math.floor(total * 1048576 / 4096));
    const latW = [], latR = []; t0 = performance.now();
    for (let i = 0; i < iter; i++) { const a = performance.now(); h.write(b4, { at: Math.floor(Math.random() * slots) * 4096 }); latW.push(performance.now() - a); if (i % 500 === 0) postMessage({ p: base + w.a * 0.5 * i / iter, f: 'Escrituras aleatorias 4K' }); }
    h.flush(); const tW4 = (performance.now() - t0) / 1000; t0 = performance.now(); const r4 = new Uint8Array(4096);
    for (let i = 0; i < iter; i++) { const a = performance.now(); h.read(r4, { at: Math.floor(Math.random() * slots) * 4096 }); latR.push(performance.now() - a); if (i % 500 === 0) postMessage({ p: base + w.a * (0.5 + 0.5 * i / iter), f: 'Lecturas aleatorias 4K' }); }
    const tR4 = (performance.now() - t0) / 1000; base += w.a;
    latW.sort((a, b) => a - b); latR.sort((a, b) => a - b);
    const lat = (a, t) => ({ avg: t / iter * 1000, p50: pct(a, 0.5), p95: pct(a, 0.95), p99: pct(a, 0.99) });
    // 4) prueba sostenida (modo avanzado): reescribe el archivo durante 'sost' segundos y mide MB/s cada segundo
    const sostenido = [];
    if (sost) {
      const ini = performance.now(); let ult = ini, acum = 0, i = 0;
      while ((performance.now() - ini) / 1000 < sost) {
        h.write(buf, { at: (i % n) * B }); i++; acum += B; const now = performance.now();
        if (now - ult >= 1000) { sostenido.push({ t: (now - ini) / 1000, mbs: acum / 1048576 / ((now - ult) / 1000), fase: 'Sostenida' }); ult = now; acum = 0; }
        if (i % 4 === 0) postMessage({ p: base + (1 - base) * Math.min(1, (now - ini) / 1000 / sost), f: 'Escritura sostenida (' + Math.round((now - ini) / 1000) + ' s de ' + sost + ' s)' });
      }
      h.flush();
    }
    h.close(); await root.removeEntry('bench.tmp');
    postMessage({ fin: true, mb: total, escritura: total / tW, lectura: total / tR, iops_w: iter / tW4, iops_r: iter / tR4,
      lat_w: lat(latW, tW4), lat_r: lat(latR, tR4), linea, sostenido, sost });
  } catch (e) { try { if (h) h.close(); if (root) await root.removeEntry('bench.tmp'); } catch (_) {} postMessage({ error: String(e && e.message || e) }); }
};`;

/* ---------------- Datos del equipo ---------------- */
async function datosSistemaNav() {
  const s = { cpu: navigator.hardwareConcurrency || null, ram_gb: navigator.deviceMemory || null, idioma: navigator.language || null,
    zona: (Intl.DateTimeFormat().resolvedOptions().timeZone) || null, pantalla: screen ? `${screen.width}×${screen.height} @${window.devicePixelRatio || 1}x` : null,
    ua: navigator.userAgent, modelo: null, version_so: null, arquitectura: null, red: null, persistente: null, cuota: null, usado: null };
  try { const e = await navigator.storage.estimate(); s.cuota = e.quota; s.usado = e.usage; } catch (e) {}
  try { s.persistente = await navigator.storage.persisted(); } catch (e) {}
  try { if (navigator.connection) s.red = `${navigator.connection.effectiveType || ""} ${navigator.connection.downlink ? navigator.connection.downlink + " Mbps" : ""}`.trim() || null; } catch (e) {}
  try {
    if (navigator.userAgentData && navigator.userAgentData.getHighEntropyValues) {
      const h = await navigator.userAgentData.getHighEntropyValues(["model", "platformVersion", "architecture", "bitness"]);
      s.modelo = h.model || null; s.version_so = h.platformVersion || null; s.arquitectura = h.architecture ? h.architecture + (h.bitness ? "-" + h.bitness + "bit" : "") : null;
    }
  } catch (e) {}
  return s;
}

/* ---------------- Cálculos y hallazgos ---------------- */
function estabilidadNav(muestras) {
  if (!muestras || muestras.length < 3) return null;
  const v = muestras.map(m => m.mbs), k = Math.max(1, Math.floor(v.length * 0.2));
  const med = a => a.reduce((x, y) => x + y, 0) / a.length;
  const ini = med(v.slice(0, k)), fin = med(v.slice(-k));
  return { media: med(v), min: Math.min(...v), max: Math.max(...v), caida_pct: ini > 0 ? Math.max(0, (1 - fin / ini) * 100) : 0, muestras: v.length };
}

function evaluarNav(D) {
  const U = UMBRALES_NAV, H = [];
  const add = (unidad, sev, hallazgo, recomendacion) => H.push({ unidad, severidad: sev, hallazgo, recomendacion });
  D.unidades.forEach(u => {
    if (u.escritura != null) {
      if (u.escritura < U.escritura_crit) add(u.unidad, "Alta", `Escritura secuencial de ${u.escritura.toFixed(1)} MB/s (umbral crítico ${U.escritura_crit} MB/s).`, "Verificar el estado del medio de almacenamiento o repetir la prueba con otras aplicaciones cerradas.");
      else if (u.escritura < U.escritura_obs) add(u.unidad, "Media", `Escritura secuencial de ${u.escritura.toFixed(1)} MB/s (referencia mínima ${U.escritura_obs} MB/s).`, "Comparar con el diagnóstico completo; puede ser un disco mecánico, memoria SD/USB o un equipo con carga.");
    }
    if (u.iops_w != null) {
      if (u.iops_w < U.iops_crit) add(u.unidad, "Alta", `IOPS 4K aleatorios de escritura: ${Math.round(u.iops_w)} (umbral crítico ${U.iops_crit}).`, "Revisar el medio de almacenamiento y la carga del equipo.");
      else if (u.iops_w < U.iops_obs) add(u.unidad, "Media", `IOPS 4K aleatorios de escritura: ${Math.round(u.iops_w)} (referencia ${U.iops_obs}).`, "Comparar con el diagnóstico completo.");
    }
    if (u.lat_p99 != null) {
      if (u.lat_p99 > U.p99_crit) add(u.unidad, "Alta", `Latencia p99 de escritura 4K de ${u.lat_p99.toFixed(2)} ms (umbral crítico ${U.p99_crit} ms).`, "Investigar picos de latencia; repetir la prueba.");
      else if (u.lat_p99 > U.p99_obs) add(u.unidad, "Media", `Latencia p99 de escritura 4K de ${u.lat_p99.toFixed(2)} ms (referencia ${U.p99_obs} ms).`, "Repetir la prueba; considerar el diagnóstico completo.");
    }
    if (u.caida_pct != null) {
      if (u.caida_pct > U.caida_crit) add(u.unidad, "Alta", `El rendimiento sostenido cayó ${u.caida_pct.toFixed(0)}% durante la prueba (umbral crítico ${U.caida_crit}%).`, "Posible limitación térmica o de caché; revisar ventilación y repetir.");
      else if (u.caida_pct > U.caida_obs) add(u.unidad, "Media", `El rendimiento sostenido cayó ${u.caida_pct.toFixed(0)}% durante la prueba (referencia ${U.caida_obs}%).`, "Repetir con el equipo en reposo; revisar ventilación.");
    }
    if (u.uso_pct != null && u.uso_pct >= U.uso_obs) add(u.unidad, "Media", `La cuota de almacenamiento del navegador está usada al ${u.uso_pct.toFixed(1)}%.`, "Liberar datos del sitio o ampliar el espacio disponible.");
    add(u.unidad, "Baja", "SMART no medible desde el navegador: temperatura, horas de uso, desgaste y errores no fueron evaluados.", "Ejecutar el diagnóstico completo con el programa local si se requiere evaluar la salud del disco.");
    const sev = new Set(H.filter(h => h.unidad === u.unidad).map(h => h.severidad));
    u.conformidad = sev.has("Alta") ? "NO CONFORME" : (sev.size ? "OBSERVACIÓN" : "CONFORME");
    u.n_hallazgos = H.filter(h => h.unidad === u.unidad).length;
  });
  return H;
}

async function huellaNav(obj) {
  try {
    const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(obj)));
    return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, "0")).join("");
  } catch (e) { return "N/D"; }
}

/* RES: { so, navegador, inicio:Date, sistema, opfs, carpeta, categorias, modo } -> D (datos planos del informe) */
async function construirDatosNav(RES, nombresSO) {
  const S = RES.sistema || {}, fin = new Date(), ini = RES.inicio || fin;
  const dur = Math.max(0, Math.round((fin - ini) / 1000));
  const unidades = [];
  const cuota = S.cuota || null, usado = S.usado != null ? S.usado : null;
  if (RES.opfs) {
    const o = RES.opfs, est = estabilidadNav(o.sostenido && o.sostenido.length >= 3 ? o.sostenido : o.linea);
    unidades.push({ unidad: "Navegador", tipo: "Almacenamiento del navegador (OPFS)", modelo: `${RES.navegador} en ${nombresSO[RES.so] || RES.so}`,
      escritura: o.escritura, lectura: o.lectura, iops_w: o.iops_w, iops_r: o.iops_r, lat_avg: o.lat_w.avg, lat_p99: o.lat_w.p99, lat_r_p99: o.lat_r.p99,
      lat_w: o.lat_w, lat_r: o.lat_r, tam_mb: o.mb, sostenido_s: o.sost || 0, caida_pct: est ? est.caida_pct : null, estab: est,
      cap_total: cuota ? cuota / 1e9 : null, cap_usado: usado != null ? usado / 1e9 : null, uso_pct: cuota && usado != null ? usado / cuota * 100 : null });
  }
  if (RES.carpeta) {
    const c = RES.carpeta;
    unidades.push({ unidad: "Carpeta", tipo: "Carpeta elegida por el usuario", modelo: c.nombre, escritura: c.escritura, lectura: c.lectura, iops_w: null, iops_r: null,
      lat_avg: null, lat_p99: null, lat_r_p99: null, lat_w: null, lat_r: null, tam_mb: c.mb, sostenido_s: 0, caida_pct: null, estab: null, cap_total: null, cap_usado: null, uso_pct: null });
  }
  const hallazgos = [];
  const D = {
    version: VERSION_INFORME_NAV, titulo: "Dashboard de Rendimiento y Almacenamiento", modo: RES.modo === "avanzado" ? "Avanzado (prueba sostenida)" : "Rápido",
    auditoria: { id: Array.from(crypto.getRandomValues(new Uint8Array(4))).map(b => b.toString(16).padStart(2, "0")).join("").toUpperCase(),
      equipo: S.modelo || "N/D (el navegador no expone el nombre del equipo)", usuario: "N/D", so: `${nombresSO[RES.so] || RES.so}${S.version_so ? " " + S.version_so : ""}`,
      arquitectura: S.arquitectura || "N/D", navegador: RES.navegador, version: VERSION_INFORME_NAV, modo: RES.modo === "avanzado" ? "Avanzado (prueba sostenida)" : "Rápido",
      categorias_analizadas: RES.categorias ? "Sí" : "No", inicio: ini.toLocaleString("es"), fin: fin.toLocaleString("es"),
      duracion: `${Math.floor(dur / 60)} min ${dur % 60} s`, huella: "" },
    sistema: S, umbrales: UMBRALES_NAV, unidades, hallazgos, linea: [], categorias: null, destacados: [], notas: [] };
  const H = evaluarNav(D); H.forEach(h => hallazgos.push(h));
  const tot = unidades.filter(u => u.cap_total != null);
  D.kpis = { unidades: unidades.length, escritura_max: Math.max(0, ...unidades.map(u => u.escritura || 0)), lectura_max: Math.max(0, ...unidades.map(u => u.lectura || 0)),
    iops_max: Math.max(0, ...unidades.map(u => u.iops_w || 0)), alertas: hallazgos.filter(h => h.severidad === "Alta").length,
    cap_total: cuota ? cuota / 1e9 : 0, cap_usado: usado != null ? usado / 1e9 : 0, cap_libre: cuota && usado != null ? (cuota - usado) / 1e9 : 0 };
  if (RES.opfs) {
    RES.opfs.linea.forEach(m => D.linea.push({ unidad: "Navegador", fase: "Escritura secuencial", t: +m.t.toFixed(2), mbs: +m.mbs.toFixed(1) }));
    (RES.opfs.sostenido || []).forEach(m => D.linea.push({ unidad: "Navegador", fase: "Escritura sostenida", t: +m.t.toFixed(2), mbs: +m.mbs.toFixed(1) }));
  }
  if (RES.categorias) {
    const c = RES.categorias, suma = Object.values(c.cats).reduce((a, x) => a + x.bytes, 0) || 1;
    D.categorias = { nombre: c.nombre, total: c.total, bytes: suma, completo: c.completo, inaccesibles: c.inacc || 0, segundos: c.segundos,
      filas: CATEGORIAS.map(n => ({ categoria: n, archivos: c.cats[n].archivos, bytes: c.cats[n].bytes, pct: c.cats[n].bytes / suma * 100 })) };
  }
  const mejor = (k, etq, f) => { const u = unidades.filter(x => x[k] != null).sort((a, b) => b[k] - a[k])[0]; if (u) D.destacados.push({ indicador: etq, unidad: u.unidad, valor: f(u[k]) }); };
  mejor("lectura", "Mayor lectura secuencial", v => v.toFixed(1) + " MB/s"); mejor("escritura", "Mayor escritura secuencial", v => v.toFixed(1) + " MB/s");
  mejor("iops_w", "Más IOPS 4K (escritura)", v => Math.round(v) + " IOPS");
  const est = unidades.filter(u => u.caida_pct != null).sort((a, b) => a.caida_pct - b.caida_pct)[0]; if (est) D.destacados.push({ indicador: "Más estable en la prueba", unidad: est.unidad, valor: "caída " + est.caida_pct.toFixed(0) + " %" });
  D.notas = [
    "Este informe se generó desde el navegador: la página no tiene acceso al hardware, por eso NO incluye SMART, temperatura, horas de uso ni desgaste. Esos datos solo los entrega el diagnóstico completo con el programa local.",
    "La velocidad medida corresponde al almacenamiento visto a través del navegador (sistema de archivos privado OPFS o la carpeta elegida); incluye la sobrecarga del navegador y puede verse afectada por cachés del sistema operativo.",
    "La capacidad mostrada es la cuota que el navegador asigna a este sitio, no el tamaño físico del disco. Un navegador no puede conocer el espacio libre real de la unidad.",
    "Los umbrales aplicados son referencias generales de rendimiento definidas para este proyecto; no provienen de una norma. La latencia por operación tiene la resolución del cronómetro del navegador (≈0,1 ms).",
    "La prueba escribe un archivo temporal en el almacenamiento del navegador (o en la carpeta elegida) y lo elimina al terminar."];
  D.auditoria.huella = await huellaNav({ u: D.unidades, h: D.hallazgos, l: D.linea, c: D.categorias, s: { cpu: S.cpu, ram: S.ram_gb } });
  return D;
}

/* ---------------- Dashboard (se ejecuta DENTRO de la página del informe; no debe usar variables externas) ---------------- */
function pintarDashboard(D) {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "N/D" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const f = (v, d) => (v == null || !isFinite(v)) ? "N/D" : Number(v).toLocaleString("es", { maximumFractionDigits: d == null ? 1 : d });
  const COL = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899", "#06b6d4"];
  const CATCOL = ["#64748b", "#3b82f6", "#8b5cf6", "#ec4899", "#10b981", "#f59e0b", "#06b6d4", "#f97316", "#94a3b8"];
  const K = D.kpis;
  Chart.defaults.color = "#cbd5e1"; Chart.defaults.borderColor = "rgba(148,163,184,.15)";
  const tip = (t) => '<span class="tip" tabindex="0" data-tip="' + t + '">i</span>';
  const card = (t, v, c, ay) => '<div class="bg-slate-800/80 border border-slate-700/60 p-5 rounded-xl shadow-lg"><p class="text-xs font-medium text-slate-400 uppercase tracking-wider">' + t + (ay ? tip(ay) : "") + '</p><p class="text-3xl font-bold ' + c + ' mt-2">' + v + '</p></div>';
  $("kpis").innerHTML = card("Unidades evaluadas", K.unidades, "text-white", "Almacenamiento del navegador y, si la elegiste, la carpeta.") + card("Escritura máx.", f(K.escritura_max, 0) + ' <span class="text-sm text-slate-400">MB/s</span>', "text-emerald-400", "Mayor velocidad de escritura secuencial medida.")
    + card("Lectura máx.", f(K.lectura_max, 0) + ' <span class="text-sm text-slate-400">MB/s</span>', "text-blue-400", "Mayor velocidad de lectura secuencial medida. Puede verse favorecida por cachés del sistema.") + card("Alertas activas", K.alertas, K.alertas > 0 ? "text-rose-500" : "text-emerald-400", "Hallazgos de severidad Alta.");
  const cc = D.unidades.map((u) => u.conformidad), global = cc.indexOf("NO CONFORME") >= 0 ? "NO CONFORME" : (cc.indexOf("OBSERVACIÓN") >= 0 ? "OBSERVACIÓN" : "CONFORME");
  const nAlta = D.hallazgos.filter((h) => h.severidad === "Alta").length, nMedia = D.hallazgos.filter((h) => h.severidad === "Media").length;
  const colV = global === "CONFORME" ? "emerald" : (global === "OBSERVACIÓN" ? "amber" : "rose");
  $("veredicto").innerHTML = '<div class="flex flex-wrap items-center gap-4 bg-' + colV + '-950/40 border border-' + colV + '-500/40 rounded-xl px-5 py-4"><span class="text-xs uppercase tracking-wider text-slate-400">Veredicto</span><span class="px-3 py-1 rounded-full text-sm font-bold bg-' + colV + '-900/70 text-' + colV + '-300">' + global + '</span><span class="text-sm text-slate-300">' + D.hallazgos.length + " hallazgos · " + nAlta + " alta(s) · " + nMedia + " media(s)</span>" + tip("Resumen de la conformidad de todas las unidades frente a los umbrales del proyecto. Detalle en «Hallazgos y recomendaciones».") + '<span class="ml-auto text-xs text-amber-400">Sin SMART (modo navegador)' + tip("Un navegador no puede leer SMART (salud, temperatura, desgaste). Para eso usa el diagnóstico completo con el programa local.") + "</span></div>";
  const dest = { 0: ["🚀", "blue"], 1: ["⚡", "emerald"], 2: ["🔁", "amber"], 3: ["📈", "purple"] };
  $("destacados").innerHTML = D.destacados.map((d, i) => { const [ic, c] = dest[i % 4];
    return '<div class="bg-gradient-to-br from-' + c + '-950/60 to-slate-900 border border-' + c + '-500/30 p-5 rounded-xl shadow-lg"><div class="flex items-center gap-2 mb-2"><span class="text-lg">' + ic + '</span><h4 class="text-xs font-bold uppercase tracking-wider text-' + c + '-400">' + esc(d.indicador) + '</h4></div><p class="text-sm font-bold text-white truncate">' + esc(d.unidad) + '</p><p class="text-2xl font-black text-' + c + '-400 mt-1">' + esc(d.valor) + '</p></div>'; }).join("");
  // Rendimiento secuencial
  new Chart($("perfChart"), { type: "bar", data: { labels: D.unidades.map((u) => u.unidad), datasets: [
    { label: "Lectura (MB/s)", data: D.unidades.map((u) => u.lectura), backgroundColor: "#3b82f6", borderRadius: 4 },
    { label: "Escritura (MB/s)", data: D.unidades.map((u) => u.escritura), backgroundColor: "#10b981", borderRadius: 4 }] },
    options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true } } } });
  // Evolución en el tiempo
  const fases = [...new Set(D.linea.map((m) => m.fase))];
  if (fases.length) new Chart($("lineChart"), { type: "line", data: { datasets: fases.map((fa, i) => ({ label: fa, data: D.linea.filter((m) => m.fase === fa).map((m) => ({ x: m.t, y: m.mbs })), borderColor: COL[i], backgroundColor: COL[i] + "33", tension: .25, pointRadius: 2 })) },
    options: { responsive: true, maintainAspectRatio: false, parsing: false, scales: { x: { type: "linear", title: { display: true, text: "Segundos" } }, y: { beginAtZero: true, title: { display: true, text: "MB/s" } } } } });
  else $("lineChart").parentNode.innerHTML = '<p class="text-slate-400 text-sm">Sin datos de evolución (se requiere la prueba del navegador).</p>';
  // Capacidad
  const cg = $("capGeneral");
  if (K.cap_total > 0) {
    cg.innerHTML = '<div class="bg-slate-800/80 border border-slate-700/60 p-6 rounded-xl shadow-lg grid md:grid-cols-2 gap-6 items-center"><div class="relative h-64"><canvas id="capChart"></canvas></div><div class="text-sm text-slate-300 space-y-2"><p><b class="text-white">Cuota del navegador:</b> ' + f(K.cap_total, 2) + ' GB</p><p><b class="text-white">Usado por este sitio:</b> ' + f(K.cap_usado, 3) + ' GB</p><p><b class="text-white">Disponible para este sitio:</b> ' + f(K.cap_libre, 2) + ' GB</p><p class="text-xs text-slate-400">Es la cuota que el navegador asigna a esta página, no el tamaño físico del disco.</p></div></div>';
    new Chart($("capChart"), { type: "doughnut", data: { labels: ["Usado", "Disponible"], datasets: [{ data: [K.cap_usado, K.cap_libre], backgroundColor: ["#f59e0b", "#10b981"], borderColor: "#0f172a", borderWidth: 2 }] }, options: { responsive: true, maintainAspectRatio: false, cutout: "62%" } });
  } else cg.innerHTML = '<p class="text-slate-400 text-sm">El navegador no informó su cuota de almacenamiento.</p>';
  // Categorías
  const C = D.categorias;
  if (C) {
    $("secCat").classList.remove("hidden");
    $("catTitulo").textContent = "Uso por categoría · «" + C.nombre + "» · " + C.total.toLocaleString("es") + " archivos" + (C.completo ? "" : " (parcial)");
    const filas = C.filas;
    new Chart($("catChart"), { type: "doughnut", data: { labels: filas.map((x) => x.categoria), datasets: [{ data: filas.map((x) => x.bytes), backgroundColor: CATCOL, borderColor: "#0f172a", borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, cutout: "55%", plugins: { legend: { position: "right" } } } });
    $("catTabla").innerHTML = filas.map((x, i) => '<tr class="border-b border-slate-700/40"><td class="py-2 pr-3"><span class="inline-block w-3 h-3 rounded-sm mr-2" style="background:' + CATCOL[i] + '"></span>' + esc(x.categoria) + '</td><td class="py-2 pr-3 text-right">' + x.archivos.toLocaleString("es") + '</td><td class="py-2 pr-3 text-right">' + f(x.bytes / 1e9, 3) + ' GB</td><td class="py-2 text-right">' + f(x.pct, 1) + ' %</td></tr>').join("");
  }
  // Detalle técnico
  const badge = (c) => '<span class="px-2 py-1 rounded text-xs font-bold ' + (c === "CONFORME" ? "bg-emerald-900/60 text-emerald-300" : c === "OBSERVACIÓN" ? "bg-amber-900/60 text-amber-300" : "bg-rose-900/60 text-rose-300") + '">' + esc(c) + '</span>';
  $("detalle").innerHTML = D.unidades.map((u) => '<tr class="border-b border-slate-700/40 hover:bg-slate-700/20"><td class="p-3 font-bold text-white">' + esc(u.unidad) + '</td><td class="p-3">' + esc(u.modelo) + '<div class="text-xs text-slate-400">' + esc(u.tipo) + '</div></td><td class="p-3 text-right">' + f(u.escritura) + '</td><td class="p-3 text-right">' + f(u.lectura) + '</td><td class="p-3 text-right">' + f(u.iops_w, 0) + '</td><td class="p-3 text-right">' + f(u.iops_r, 0) + '</td><td class="p-3 text-right">' + f(u.lat_avg, 3) + '</td><td class="p-3 text-right">' + f(u.lat_p99, 3) + '</td><td class="p-3 text-right">' + (u.caida_pct == null ? "N/D" : f(u.caida_pct, 0) + " %") + '</td><td class="p-3 text-slate-400">N/D</td><td class="p-3">' + badge(u.conformidad) + '</td></tr>').join("");
  $("hall").innerHTML = D.hallazgos.length ? D.hallazgos.map((h) => '<tr class="border-b border-slate-700/40"><td class="p-3 font-bold text-white">' + esc(h.unidad) + '</td><td class="p-3"><span class="px-2 py-1 rounded text-xs font-bold ' + (h.severidad === "Alta" ? "bg-rose-900/60 text-rose-300" : h.severidad === "Media" ? "bg-amber-900/60 text-amber-300" : "bg-blue-900/60 text-blue-300") + '">' + esc(h.severidad) + '</span></td><td class="p-3">' + esc(h.hallazgo) + '</td><td class="p-3 text-slate-300">' + esc(h.recomendacion) + '</td></tr>').join("") : '<tr><td class="p-3" colspan="4">Sin hallazgos.</td></tr>';
  $("nHall").textContent = "(" + D.hallazgos.length + ")"; $("nNotas").textContent = "(" + D.notas.length + ")";
  if (nAlta + nMedia > 0) $("detHall").open = true;
  $("notas").innerHTML = D.notas.map((n) => "<li>" + esc(n) + "</li>").join("");
  const A = D.auditoria; $("audit").textContent = "ID " + A.id + " · " + A.inicio + " · duración " + A.duracion + " · huella SHA-256 " + String(A.huella).slice(0, 16) + "…";
  $("subtitulo").textContent = A.navegador + " en " + A.so + " · modo " + A.modo;
}

function htmlInformeNav(D, incluirExcel) {
  const datos = JSON.stringify(D).replace(/</g, "\\u003c");
  const th = (t, r, ay) => '<th class="p-3 ' + (r ? "text-right " : "") + 'text-xs font-semibold text-slate-400 uppercase tracking-wider">' + t + (ay ? '<span class="tip" tabindex="0" data-tip="' + ay + '">i</span>' : "") + "</th>";
  return `<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard de rendimiento (modo navegador)</title>
<style>${TIPS_CSS} summary{list-style:none} summary::-webkit-details-marker{display:none} summary::before{content:"▸";margin-right:8px;color:#64748b} details[open]>summary::before{content:"▾"}</style>
<script src="https://cdn.tailwindcss.com"><\/script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js"><\/script>
<script src="https://cdn.jsdelivr.net/npm/xlsx-js-style@1.2.0/dist/xlsx.bundle.js"><\/script>
</head>
<body class="bg-slate-900 text-slate-100 font-sans antialiased">
<div class="max-w-7xl mx-auto px-4 py-8">
  <header class="mb-8 border-b border-slate-800 pb-6 flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
    <div><h1 class="text-3xl font-extrabold tracking-tight text-white">${D.titulo}</h1>
      <p class="text-slate-400 mt-1"><span id="subtitulo"></span> <span class="text-xs text-slate-500">· Modo navegador</span></p></div>
    <button onclick="descargarExcel()" class="bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold px-4 py-2.5 rounded-lg border border-emerald-500 shadow-lg">📥 Descargar Reporte Excel</button>
  </header>
  <div id="veredicto" class="mb-6"></div>
  <div id="kpis" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6"></div>
  <div id="destacados" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8"></div>
  <div class="grid grid-cols-1 lg:grid-cols-2 gap-8 mb-8">
    <div class="bg-slate-800/80 border border-slate-700/60 p-6 rounded-xl shadow-lg"><h3 class="text-lg font-bold text-white mb-4">Evolución del rendimiento<span class="tip" tabindex="0" data-tip="MB/s medidos durante la escritura. En modo avanzado incluye una fase sostenida: si la curva baja, el equipo limita el rendimiento (calor o caché llena).">i</span></h3><div class="relative h-72"><canvas id="lineChart"></canvas></div></div>
    <div class="bg-slate-800/80 border border-slate-700/60 p-6 rounded-xl shadow-lg"><h3 class="text-lg font-bold text-white mb-4">Rendimiento Secuencial (MB/s)<span class="tip" tabindex="0" data-tip="Lectura y escritura de bloques grandes (4 MB). Indica la velocidad para copiar archivos grandes.">i</span></h3><div class="relative h-72"><canvas id="perfChart"></canvas></div></div>
  </div>
  <div class="mb-8"><h3 class="text-xl font-bold text-white mb-4">Capacidad<span class="tip" tabindex="0" data-tip="Es la cuota que el navegador asigna a esta página, no el tamaño del disco físico. Un navegador no puede conocer el espacio libre real.">i</span></h3><div id="capGeneral"></div></div>
  <div id="secCat" class="mb-8 hidden"><h3 id="catTitulo" class="text-xl font-bold text-white mb-4"></h3>
    <div class="bg-slate-800/80 border border-slate-700/60 p-6 rounded-xl shadow-lg grid lg:grid-cols-2 gap-6 items-center"><div class="relative h-72"><canvas id="catChart"></canvas></div>
    <table class="w-full text-sm"><thead><tr class="border-b border-slate-700">${th("Categoría")}${th("Archivos", 1)}${th("Tamaño", 1)}${th("%", 1)}</tr></thead><tbody id="catTabla"></tbody></table></div></div>
  <div class="bg-slate-800/80 border border-slate-700/60 rounded-xl shadow-lg overflow-hidden mb-8"><div class="p-6 border-b border-slate-700/60"><h3 class="text-lg font-bold text-white">Detalle Técnico de Unidades</h3></div>
    <div class="overflow-x-auto"><table class="w-full text-left text-sm"><thead class="bg-slate-900/60"><tr>${th("Unidad")}${th("Modelo / tipo")}${th("Escritura MB/s", 1, "Velocidad de escritura secuencial (bloques de 4 MB).")}${th("Lectura MB/s", 1, "Velocidad de lectura secuencial (bloques de 4 MB).")}${th("IOPS 4K esc.", 1, "Escrituras aleatorias de 4 KB por segundo: mide la agilidad con muchos archivos pequeños.")}${th("IOPS 4K lect.", 1, "Lecturas aleatorias de 4 KB por segundo.")}${th("Lat. media ms", 1, "Tiempo promedio por operación de escritura 4K, en milisegundos. Menos es mejor.")}${th("Lat. p99 ms", 1, "El 99 % de las operaciones tardó menos que este valor. Detecta picos de lentitud.")}${th("Caída sostenida", 1, "Cuánto baja el rendimiento entre el inicio y el final de la prueba sostenida (modo avanzado).")}${th("SMART", 0, "Salud del disco. No medible desde el navegador.")}${th("Conformidad", 0, "Resultado frente a los umbrales de referencia del proyecto.")}</tr></thead><tbody id="detalle"></tbody></table></div></div>
  <details id="detHall" class="bg-slate-800/80 border border-slate-700/60 rounded-xl shadow-lg overflow-hidden mb-8"><summary class="p-6 cursor-pointer text-lg font-bold text-white">Hallazgos y recomendaciones <span id="nHall" class="text-sm font-normal text-slate-400"></span></summary>
    <div class="overflow-x-auto"><table class="w-full text-left text-sm"><thead class="bg-slate-900/60"><tr>${th("Unidad")}${th("Severidad")}${th("Hallazgo")}${th("Recomendación")}</tr></thead><tbody id="hall"></tbody></table></div></details>
  <details class="bg-slate-800/60 border border-slate-700/60 rounded-xl mb-8"><summary class="p-6 cursor-pointer text-lg font-bold text-white">Notas y limitaciones <span id="nNotas" class="text-sm font-normal text-slate-400"></span></summary><ul id="notas" class="list-disc ml-11 mr-6 pb-6 space-y-1 text-sm text-slate-300"></ul></details>
  <p id="audit" class="text-xs text-slate-500 text-center"></p>
</div>
<script>
const DATOS_EXCEL = ${datos};
${pintarDashboard.toString()}
${iniciarTips.toString()}
${construirExcelNav.toString()}
function descargarExcel() { const wb = construirExcelNav(DATOS_EXCEL, XLSX); XLSX.writeFile(wb, "Reporte_Diagnostico_Navegador_" + DATOS_EXCEL.auditoria.id + ".xlsx"); }
window.addEventListener("load", function () { iniciarTips(); try { pintarDashboard(DATOS_EXCEL); } catch (e) { document.body.insertAdjacentHTML("afterbegin", '<p style="color:#fecaca;padding:12px">No se pudieron cargar los gráficos (se necesita conexión para Chart.js): ' + e.message + "</p>"); } });
<\/script></body></html>`;
}

/* ---------------- Excel (misma presentación que el informe del programa local) ---------------- */
function construirExcelNav(D, XLSX) {
  const A = D.auditoria, U = D.umbrales, K = D.kpis, S = D.sistema || {};
  const borde = { style: "thin", color: { rgb: "D9D9D9" } }, bordes = { top: borde, bottom: borde, left: borde, right: borde };
  const centro = { horizontal: "center", vertical: "center", wrapText: true }, izq = { horizontal: "left", vertical: "center", wrapText: true };
  const T = (v) => ({ v, t: "s", s: { font: { name: "Calibri", sz: 16, bold: true, color: { rgb: "1F4E78" } } } });
  const Sx = (v) => ({ v, t: "s", s: { font: { name: "Calibri", sz: 12, bold: true, color: { rgb: "1F4E78" } }, border: { bottom: { style: "medium", color: { rgb: "1F4E78" } } } } });
  const H = (v) => ({ v, t: "s", s: { font: { name: "Calibri", sz: 11, bold: true, color: { rgb: "FFFFFF" } }, fill: { fgColor: { rgb: "1F4E78" } }, alignment: centro, border: bordes } });
  const C = (v, x) => {
    x = x || {}; if (v === null || v === undefined || v === "") v = "N/D";
    const num = typeof v === "number" && isFinite(v);
    const c = { v, t: num ? "n" : "s", s: { font: Object.assign({ name: "Calibri", sz: 10 }, x.font || {}), alignment: x.align || centro, border: bordes } };
    if (x.fill) c.s.fill = { fgColor: { rgb: x.fill } }; if (x.z && num) c.z = x.z; return c;
  };
  const L = (v) => C(v, { font: { bold: true }, fill: "EEF3F8", align: izq });
  const V = (v, x) => C(v, Object.assign({ align: izq }, x || {}));
  const VERDE = { fill: "D1FAE5", font: { bold: true, color: { rgb: "065F46" } } }, AMARILLO = { fill: "FEF3C7", font: { bold: true, color: { rgb: "92400E" } } };
  const ROJO = { fill: "FEE2E2", font: { bold: true, color: { rgb: "991B1B" } } }, AZUL = { fill: "DBEAFE", font: { bold: true, color: { rgb: "1E40AF" } } };
  const estiloConf = (c) => c === "CONFORME" ? VERDE : (c === "OBSERVACIÓN" ? AMARILLO : ROJO);
  const estiloSev = (s) => s === "Alta" ? ROJO : (s === "Media" ? AMARILLO : AZUL);
  const auto = (filas) => { const n = Math.max(...filas.map((f) => f.length)); const a = []; for (let c = 0; c < n; c++) { let m = 10; filas.forEach((f) => { const x = f[c]; if (x && x.v !== undefined) m = Math.max(m, String(x.v).length + 3); }); a.push({ wch: Math.min(m, 60) }); } return a; };
  const hoja = (filas, ex) => { ex = ex || {}; const ws = XLSX.utils.aoa_to_sheet(filas); ws["!cols"] = (ex.anchos || auto(filas)).map((a) => typeof a === "number" ? { wch: a } : a);
    if (ex.filtro) ws["!autofilter"] = { ref: ex.filtro }; if (ex.merges) ws["!merges"] = ex.merges; if (ex.filas) ws["!rows"] = ex.filas; return ws; };
  const agregar = (wb, n, ws) => { wb.SheetNames.push(n); wb.Sheets[n] = ws; };
  const rango = (filas, cols) => "A1:" + XLSX.utils.encode_cell({ r: filas.length - 1, c: cols - 1 });
  const GB = '#,##0.000" GB"', PCT = '0.0%';
  const wb = XLSX.utils.book_new();

  // 1. RESUMEN
  const res = [], merges = [], altos = [];
  const fila = function () { res.push(Array.prototype.slice.call(arguments)); return res.length - 1; };
  fila(T("Reporte de Diagnóstico de Almacenamiento (modo navegador)"));
  fila(V("Evidencia técnica de apoyo para auditoría. Esta versión no incluye SMART: ver «Notas y limitaciones».", { font: { italic: true, color: { rgb: "64748B" } } }));
  fila(); fila(Sx("Datos de la auditoría"), Sx(""));
  [["ID de ejecución", A.id], ["Equipo", A.equipo], ["Sistema operativo", A.so], ["Arquitectura", A.arquitectura], ["Navegador", A.navegador], ["Versión del informe", A.version],
   ["Modo de prueba", A.modo], ["Análisis por categorías", A.categorias_analizadas], ["Inicio", A.inicio], ["Fin", A.fin], ["Duración", A.duracion], ["Huella SHA-256 de los datos", A.huella]].forEach((p) => fila(L(p[0]), V(p[1])));
  fila(); fila(Sx("Criterios y umbrales aplicados (referencias del proyecto, no normativas)"), Sx(""));
  [["Escritura secuencial (observación / crítico)", U.escritura_obs + " / " + U.escritura_crit + " MB/s"], ["IOPS 4K de escritura (observación / crítico)", U.iops_obs + " / " + U.iops_crit],
   ["Latencia p99 4K (observación / crítico)", U.p99_obs + " / " + U.p99_crit + " ms"], ["Caída del rendimiento sostenido (observación / crítico)", U.caida_obs + " / " + U.caida_crit + " %"],
   ["Uso de la cuota (observación)", U.uso_obs + " %"]].forEach((p) => fila(L(p[0]), V(p[1])));
  fila(); fila(Sx("Indicadores generales"), Sx(""));
  fila(L("Unidades evaluadas"), V(K.unidades)); fila(L("Escritura máxima"), V(K.escritura_max, { z: '#,##0.0" MB/s"' })); fila(L("Lectura máxima"), V(K.lectura_max, { z: '#,##0.0" MB/s"' }));
  fila(L("Alertas activas"), V(K.alertas, K.alertas > 0 ? ROJO : VERDE)); fila(L("Hallazgos registrados"), V(D.hallazgos.length, D.hallazgos.length > 0 ? AMARILLO : VERDE));
  fila(); fila(Sx("Capacidad (cuota del navegador, no el disco físico)"), Sx(""));
  fila(L("Cuota total"), V(K.cap_total, { z: GB })); fila(L("Usado por este sitio"), V(K.cap_usado, { z: GB })); fila(L("Disponible para este sitio"), V(K.cap_libre, { z: GB }));
  fila(L("Uso de la cuota"), V(K.cap_total ? K.cap_usado / K.cap_total : 0, { z: PCT }));
  fila(); fila(Sx("Conformidad por unidad"), Sx(""), Sx(""), Sx(""));
  fila(H("Unidad"), H("Modelo"), H("Conformidad"), H("N.º de hallazgos"));
  D.unidades.forEach((u) => fila(C(u.unidad, { font: { bold: true } }), C(u.modelo), C(u.conformidad, estiloConf(u.conformidad)), C(u.n_hallazgos)));
  fila(); fila(Sx("Destacados"), Sx(""), Sx("")); fila(H("Indicador"), H("Unidad"), H("Valor"));
  D.destacados.forEach((d) => fila(L(d.indicador), C(d.unidad), C(d.valor)));
  fila(); fila(Sx("Hallazgos y recomendaciones"), Sx(""), Sx(""), Sx("")); fila(H("Unidad"), H("Severidad"), H("Hallazgo"), H("Recomendación"));
  D.hallazgos.forEach((h) => fila(C(h.unidad, { font: { bold: true } }), C(h.severidad, estiloSev(h.severidad)), C(h.hallazgo, { align: izq }), C(h.recomendacion, { align: izq })));
  fila(); fila(Sx("Notas y limitaciones"), Sx(""), Sx(""), Sx(""));
  D.notas.forEach((n) => { const r = fila(V(n)); merges.push({ s: { r, c: 0 }, e: { r, c: 3 } }); altos[r] = { hpx: 48 }; });
  agregar(wb, "Resumen", hoja(res, { anchos: [40, 40, 58, 58], merges, filas: altos.map((a) => a || {}) }));

  // 2. INVENTARIO DE ACTIVOS (el navegador expone poco del equipo)
  const inv = [[H("Atributo"), H("Valor"), H("Origen")]];
  [["Sistema operativo", A.so, "navigator.userAgent / userAgentData"], ["Modelo del dispositivo", S.modelo, "userAgentData (solo algunos navegadores Android/ChromeOS)"],
   ["Arquitectura", A.arquitectura, "userAgentData"], ["Navegador", A.navegador, "navigator.userAgent"], ["Núcleos lógicos de CPU", S.cpu, "navigator.hardwareConcurrency"],
   ["Memoria RAM aproximada (GB)", S.ram_gb, "navigator.deviceMemory (valor redondeado, máx. 8)"], ["Resolución de pantalla", S.pantalla, "screen"], ["Idioma", S.idioma, "navigator.language"],
   ["Zona horaria", S.zona, "Intl"], ["Conexión de red", S.red, "navigator.connection"], ["Almacenamiento persistente concedido", S.persistente == null ? "N/D" : (S.persistente ? "Sí" : "No"), "navigator.storage.persisted()"],
   ["Agente de usuario", S.ua, "navigator.userAgent"]].forEach((p) => inv.push([L(p[0]), V(p[1]), V(p[2], { font: { color: { rgb: "64748B" } } })]));
  agregar(wb, "Inventario de Activos", hoja(inv, { anchos: [38, 60, 56] }));

  // 3. DETALLE TÉCNICO
  const cab = ["Unidad", "Tipo", "Modelo", "Escritura seq. (MB/s)", "Lectura seq. (MB/s)", "IOPS 4K escritura", "IOPS 4K lectura", "Latencia media esc. (ms)", "Latencia p50 (ms)", "Latencia p95 (ms)", "Latencia p99 (ms)", "Tamaño de prueba (MB)", "Prueba sostenida (s)", "Caída sostenida (%)", "SMART", "Conformidad", "N.º hallazgos"];
  const det = [cab.map(H)];
  D.unidades.forEach((u) => { const lw = u.lat_w || {}; det.push([C(u.unidad, { font: { bold: true } }), C(u.tipo), C(u.modelo), C(u.escritura == null ? null : +u.escritura.toFixed(1), { z: "#,##0.0" }), C(u.lectura == null ? null : +u.lectura.toFixed(1), { z: "#,##0.0" }),
    C(u.iops_w == null ? null : Math.round(u.iops_w), { z: "#,##0" }), C(u.iops_r == null ? null : Math.round(u.iops_r), { z: "#,##0" }), C(u.lat_avg == null ? null : +u.lat_avg.toFixed(3)), C(lw.p50 == null ? null : +lw.p50.toFixed(3)), C(lw.p95 == null ? null : +lw.p95.toFixed(3)),
    C(u.lat_p99 == null ? null : +u.lat_p99.toFixed(3)), C(u.tam_mb), C(u.sostenido_s || null), C(u.caida_pct == null ? null : +u.caida_pct.toFixed(1)), C("N/D (no medible)"), C(u.conformidad, estiloConf(u.conformidad)), C(u.n_hallazgos)]); });
  agregar(wb, "Detalle Técnico", hoja(det, { filtro: rango(det, cab.length) }));

  // 4. CAPACIDAD
  const cap = [[H("Concepto"), H("Valor (GB)"), H("% de la cuota")]];
  cap.push([L("Cuota total del navegador"), C(K.cap_total, { z: GB }), C(1, { z: PCT })]);
  cap.push([L("Usado por este sitio"), C(K.cap_usado, { z: GB }), C(K.cap_total ? K.cap_usado / K.cap_total : 0, { z: PCT })]);
  cap.push([L("Disponible para este sitio"), C(K.cap_libre, { z: GB }), C(K.cap_total ? K.cap_libre / K.cap_total : 0, { z: PCT })]);
  agregar(wb, "Capacidad", hoja(cap, { anchos: [38, 22, 22] }));

  // 5. CATEGORÍAS
  if (D.categorias) {
    const c = D.categorias, ct = [[T("Uso por categoría de archivos · " + c.nombre)], [], [H("Categoría"), H("Archivos"), H("Tamaño (bytes)"), H("Tamaño (GB)"), H("% del total")]];
    c.filas.forEach((x) => ct.push([L(x.categoria), C(x.archivos, { z: "#,##0" }), C(x.bytes, { z: "#,##0" }), C(x.bytes / 1e9, { z: GB }), C(x.pct / 100, { z: PCT })]));
    ct.push([L("TOTAL"), C(c.total, { z: "#,##0" }), C(c.bytes, { z: "#,##0" }), C(c.bytes / 1e9, { z: GB }), C(1, { z: PCT })]);
    ct.push([]); ct.push([V("Archivos inaccesibles: " + c.inaccesibles + " · análisis " + (c.completo ? "completo" : "parcial") + " · " + (c.segundos || 0).toFixed(1) + " s")]);
    agregar(wb, "Categorías de Archivos", hoja(ct, { anchos: [38, 16, 22, 18, 16] }));
  }

  // 6. EVOLUCIÓN DEL RENDIMIENTO (equivalente de «Evolución Térmica»)
  const ev = [[H("Unidad"), H("Fase"), H("Tiempo (s)"), H("Rendimiento (MB/s)")]];
  if (D.linea.length) D.linea.forEach((m) => ev.push([C(m.unidad), C(m.fase), C(m.t), C(m.mbs)])); else ev.push([C("—"), C("—"), C("—"), C("Sin muestras")]);
  agregar(wb, "Evolución del Rendimiento", hoja(ev, { filtro: rango(ev, 4) }));

  // 7. REGISTRO SMART (todo N/D con su motivo, mismo formato que el programa local)
  const sm = [["Unidad", "Parámetro", "Valor", "Estado", "Criterio"].map(H)];
  D.unidades.forEach((u) => ["Temperatura actual (°C)", "Horas de encendido", "Desgaste / vida útil consumida (%)", "Errores de lectura", "Errores de escritura"].forEach((p) =>
    sm.push([C(u.unidad, { font: { bold: true } }), L(p), C("N/D"), C("INFO"), V("SMART no medible desde el navegador: usar el diagnóstico completo", { font: { color: { rgb: "64748B" } } })])));
  agregar(wb, "Registro SMART", hoja(sm, { filtro: rango(sm, 5), anchos: [14, 38, 14, 12, 62] }));
  return wb;
}
