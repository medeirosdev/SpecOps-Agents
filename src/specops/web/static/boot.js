/* Applied before first paint so a saved theme doesn't flash. A file, not inline: the page's CSP allows no inline scripts. */
try { const t = localStorage.getItem("specops.theme"); if (t) document.documentElement.dataset.theme = t; } catch {}
