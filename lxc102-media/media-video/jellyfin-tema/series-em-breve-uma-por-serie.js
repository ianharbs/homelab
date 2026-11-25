// "Séries em breve" (Home Screen Sections): uma entrada por série em vez de uma por episódio.
// Fica o card do próximo episódio (a seção já vem em ordem de data) e ganha "(+N ep.)" se houver mais na janela.
// Instalado no plugin JavaScript Injector (Painel › Plugins › JavaScript Injector), 25/11/2026.
(() => {
    'use strict';
    function agrupar() {
        const grupos = new Map();  // container da seção -> cards
        document.querySelectorAll('.upcoming-show-card').forEach(card => {
            const box = card.parentElement;
            if (!grupos.has(box)) grupos.set(box, []);
            grupos.get(box).push(card);
        });
        grupos.forEach(cards => {
            const vistos = new Map();
            cards.forEach(card => {
                const linhas = card.querySelectorAll('.cardText');
                const serie = (linhas[0] && linhas[0].textContent.trim()) || '';
                if (!serie) return;
                if (!vistos.has(serie)) {
                    vistos.set(serie, { card, extra: 0, linha: linhas[1] });
                    card.style.display = '';
                } else {
                    vistos.get(serie).extra += 1;
                    card.style.display = 'none';
                }
            });
            vistos.forEach(v => {
                if (!v.linha) return;
                const base = v.linha.dataset.ihomeBase || v.linha.textContent.trim();
                v.linha.dataset.ihomeBase = base;
                const texto = v.extra ? `${base} (+${v.extra} ep.)` : base;
                if (v.linha.textContent !== texto) v.linha.textContent = texto;
            });
        });
    }
    let agendado = false;
    new MutationObserver(() => {
        if (agendado) return;
        agendado = true;
        setTimeout(() => { agendado = false; agrupar(); }, 150);
    }).observe(document.documentElement, { childList: true, subtree: true });
})();
