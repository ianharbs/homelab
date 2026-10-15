document.addEventListener('click', function (e) {
  var t = e.target;
  if (t && t.closest && t.closest('img[alt="logo"]')) {
    document.body.classList.toggle('show-hidden');
  }
});
