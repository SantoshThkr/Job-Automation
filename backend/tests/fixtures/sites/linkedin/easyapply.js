// Mock of LinkedIn's Easy Apply modal: steps, validation, review, submit, discard.
(function () {
  const modal = document.querySelector('.jobs-easy-apply-modal');
  const steps = Array.from(modal.querySelectorAll('.ea-step'));
  let i = 0;
  const next = modal.querySelector('[aria-label="Continue to next step"]');
  const review = modal.querySelector('[aria-label="Review your application"]');
  const submit = modal.querySelector('[aria-label="Submit application"]');
  const err = modal.querySelector('.artdeco-inline-feedback--error');
  function show() {
    steps.forEach((s, k) => s.hidden = k !== i);
    modal.querySelector('progress').value = Math.round(100 * i / (steps.length - 1));
    const last = i === steps.length - 1;
    next.hidden = i >= steps.length - 2; review.hidden = i !== steps.length - 2; submit.hidden = !last;
  }
  function valid() {
    for (const el of steps[i].querySelectorAll('[required]')) {
      if (el.type === 'radio') { if (!steps[i].querySelector('input[name="' + el.name + '"]:checked')) return false; }
      else if (el.type === 'file') { if (!el.files.length && !steps[i].querySelector('.jobs-document-upload-redesign-card__container--selected')) return false; }
      else if (!el.value.trim()) return false;
    }
    return true;
  }
  function go() { if (!valid()) { err.hidden = false; return; } err.hidden = true; i++; show(); }
  next.onclick = go; review.onclick = go;
  submit.onclick = () => { window.SUBMITTED = true; modal.hidden = true; document.getElementById('post-apply').hidden = false;
    document.querySelector('.jobs-apply-button').outerHTML = '<div class="artdeco-inline-feedback artdeco-inline-feedback--success">Application submitted</div>'; };
  modal.querySelector('[aria-label="Dismiss"]').onclick = () => { document.getElementById('discard-dialog').hidden = false; };
  document.querySelector('[data-control-name="discard_application_confirm_btn"]').onclick = () => {
    window.DISCARDED = true; document.getElementById('discard-dialog').hidden = true; modal.hidden = true; i = 0; show(); };
  document.querySelector('#post-apply [aria-label="Dismiss"]').onclick = () => { document.getElementById('post-apply').hidden = true; };
  modal.querySelectorAll('.jobs-document-upload-redesign-card__container').forEach(c => c.onclick = () => {
    modal.querySelectorAll('.jobs-document-upload-redesign-card__container').forEach(x => x.classList.remove('jobs-document-upload-redesign-card__container--selected'));
    c.classList.add('jobs-document-upload-redesign-card__container--selected'); window.SELECTED_RESUME = c.innerText.trim(); });
  window.openEasyApply = () => { modal.hidden = false; show(); };
  show();
})();
