"""A page that shows the feed runs in AG Grid Community: sortable, filterable, refreshed while a run is going.

It reads the same /api/feed-runs/ endpoint a script would, so the page holds no data of its own. The grid library is pinned to
one version and loaded from a CDN; nothing here needs a build step."""
from django.http import HttpResponse

AG_GRID_VERSION = "33.3.2"

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Camlobe feed runs</title>
<script src="https://cdn.jsdelivr.net/npm/ag-grid-community@__VERSION__/dist/ag-grid-community.min.js"></script>
<style>
 body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#fafafa;color:#1a1a1a}
 h1{font-size:18px;margin:0 0 4px} p{margin:0 0 12px;color:#555;font-size:14px}
 #grid{height:70vh;min-height:320px}
 .st-running{color:#8a5a00;font-weight:600}.st-failed{color:#a11;font-weight:600}.st-succeeded{color:#176b2c}
</style></head><body>
<h1>Feed runs</h1>
<p id="note">Loading runs. A run that is still going refreshes every few seconds.</p>
<div id="grid" class="ag-theme-quartz"></div>
<script>
const COLS = [
 {field:"id", width:100, sort:"desc", filter:"agNumberColumnFilter"},
 {field:"kind", width:95},
 {field:"supplier", width:110},
 {field:"status", width:120, cellClass:p => "st-" + p.value},
 {headerName:"read", valueGetter:p => p.data.counts.read, width:90, filter:"agNumberColumnFilter"},
 {headerName:"written", valueGetter:p => p.data.counts.written, width:100, filter:"agNumberColumnFilter"},
 {headerName:"unchanged", valueGetter:p => p.data.counts.unchanged, width:140, filter:"agNumberColumnFilter"},
 {headerName:"rejected", valueGetter:p => p.data.counts.rejected, width:100, filter:"agNumberColumnFilter"},
 {headerName:"started", field:"started_at", width:160, valueFormatter:p => (p.value || "").slice(0, 19).replace("T", " ")},
 {headerName:"first error", valueGetter:p => (p.data.errors[0] || {}).message || "", flex:1, minWidth:260, tooltipField:"source_ref"},
];
const api = agGrid.createGrid(document.getElementById("grid"), {
 columnDefs: COLS, defaultColDef: {sortable:true, filter:true, resizable:true}, getRowId: p => String(p.data.id), animateRows:false,
});
async function load() {
 const res = await fetch("/api/feed-runs/?limit=100");
 const body = await res.json();
 const rows = body.results || body;
 api.setGridOption("rowData", rows);
 const running = rows.filter(r => r.status === "running").length;
 document.getElementById("note").textContent = rows.length + " runs" + (running ? ", " + running + " running now" : "");
 return running;
}
(async function tick(){ let running = 0; try { running = await load(); } catch (e) { document.getElementById("note").textContent = "Could not load the runs: " + e; } setTimeout(tick, running ? 3000 : 30000); })();
</script></body></html>
""".replace("__VERSION__", AG_GRID_VERSION)


def feed_runs_page(request):
    return HttpResponse(PAGE, content_type="text/html; charset=utf-8")
