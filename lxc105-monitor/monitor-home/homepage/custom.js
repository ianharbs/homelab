// Grupos ocultos por padrão (nome exato do grupo no services.yaml).
var GRUPOS_OCULTOS = ['Mídia — XXX'];

function marcarGruposOcultos() {
  document.querySelectorAll('.services-group').forEach(function (g) {
    var h = g.querySelector(':scope h2.service-group-name');
    if (h && GRUPOS_OCULTOS.indexOf(h.textContent.trim()) !== -1) g.classList.add('grupo-oculto');
  });
}
marcarGruposOcultos();
new MutationObserver(marcarGruposOcultos).observe(document.body, { childList: true, subtree: true });

document.addEventListener('click', function (e) {
  var t = e.target;
  if (t && t.closest && t.closest('img[alt="logo"]')) {
    document.body.classList.toggle('show-hidden');
  }
});
