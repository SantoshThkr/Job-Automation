// Mock of the 2026 LinkedIn Easy Apply dialog, modelled on the live page of job 4465524746:
// native <dialog open data-testid="dialog" aria-labelledby="dialog-header"> without a role attribute, a "1/4 pages"
// counter instead of <progress>, the Next button hooked by data-view-name="continue-unify" (no aria-label),
// Review / Submit buttons identified only by their text, generated class names everywhere.
// ?noconfirm=1 : submitting closes the dialog without any confirmation (LinkedIn did not confirm).
(function () {
  const dlg = document.querySelector('dialog[data-testid="dialog"]');
  const discardDlg = document.getElementById('discard-dlg');
  const steps = Array.from(dlg.querySelectorAll('section._st'));
  const next = dlg.querySelector('[data-view-name="continue-unify"]');
  const review = dlg.querySelector('button._rv');
  const submit = dlg.querySelector('button._sb');
  const err = dlg.querySelector('._err');
  const noConfirm = new URLSearchParams(location.search).has('noconfirm');
  let i = 0;
  function show() {
    steps.forEach((s, k) => s.hidden = k !== i);
    dlg.querySelector('._pg').textContent = (i + 1) + '/' + steps.length + ' pages';
    next.hidden = i >= steps.length - 2; review.hidden = i !== steps.length - 2; submit.hidden = i !== steps.length - 1;
  }
  function valid() {
    for (const el of steps[i].querySelectorAll('[required]')) {
      if (el.type === 'radio') { if (!steps[i].querySelector('input[name="' + el.name + '"]:checked')) return false; }
      else if (el.type === 'file') { if (!el.files.length) return false; }
      else if (!el.value.trim() || (el.checkValidity && !el.checkValidity())) return false;
    }
    return true;
  }
  function go() { if (!valid()) { err.hidden = false; return; } err.hidden = true; i++; show(); }
  next.onclick = go; review.onclick = go;
  submit.onclick = () => {
    window.SUBMITTED = true;
    window.SUBMITTED_DATA = window.kitData ? kitData(dlg) : Object.fromEntries(Array.from(dlg.querySelectorAll('input, select'))
      .filter(e => e.type !== 'radio' || e.checked).map(e => [e.type === 'radio' ? e.name : (e.id || e.name), e.type === 'checkbox' ? e.checked : e.value]));
    if (noConfirm) { dlg.close(); return; }
    dlg.querySelector('form').hidden = true; dlg.querySelector('._ft').hidden = true; dlg.querySelector('._pg').hidden = true;
    dlg.querySelector('._done').hidden = false;
    document.querySelector('[data-view-name="job-apply-button"]').outerHTML = '<span class="_ap_3">Applied 1 minute ago</span>';
  };
  dlg.querySelector('button[aria-label="Dismiss"]').onclick = () => {
    if (window.SUBMITTED) { dlg.close(); return; }
    discardDlg.showModal();
  };
  dlg.querySelector('._done button').onclick = () => dlg.close();
  discardDlg.querySelector('button._dc').onclick = () => {
    window.DISCARDED = true; discardDlg.close(); dlg.close(); i = 0; show();
  };
  discardDlg.querySelector('button._sv').onclick = () => { window.SAVED_DRAFT = true; discardDlg.close(); dlg.close(); };
  document.querySelector('[data-view-name="job-apply-button"]').onclick = () => { dlg.showModal(); show(); };
  show();
})();
