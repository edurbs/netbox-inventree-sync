/*
 * Field-pair builder — progressive enhancement for mapping_builder.html.
 *
 * The server already renders the pairs table, both selects and the
 * initial compatibility greying, and it re-validates every save
 * (validate_mapping is the gate; this script's greying is UX only).
 * Here we add: dynamic greying of the InvenTree options as the NetBox
 * selection changes, the pointer sub-attribute select (pointer field +
 * string target), adding/removing linked rows, and keeping the hidden
 * `pairs` JSON input in sync so a plain form submit posts the full
 * list. Compatibility verdicts come from the option elements
 * themselves (data-compatible / data-type), which the server derives
 * from the embedded compatible_netbox_fields lists. Without JavaScript
 * the page degrades gracefully: the current pairs are shown and a save
 * re-posts them unchanged.
 */
(() => {
  'use strict';

  const form = document.getElementById('mapping-builder-form');
  if (!form) return;

  const pairsInput = document.getElementById('pairs-json');
  const pairsBody = document.getElementById('pairs-body');
  const netboxSelect = document.getElementById('netbox-field-select');
  const inventreeSelect = document.getElementById('inventree-field-select');
  const subfieldContainer = document.getElementById('subfield-select-container');
  const subfieldSelect = document.getElementById('netbox-subfield-select');
  const addButton = document.getElementById('add-pair');

  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));

  const selectedNetbox = () => netboxSelect.selectedOptions[0] || null;
  const selectedInventree = () => inventreeSelect.selectedOptions[0] || null;

  /* The hidden input always carries the full pair list as JSON. */
  const syncPayload = () => {
    pairsInput.value = JSON.stringify(
      Array.from(pairsBody.querySelectorAll('tr.pair-row')).map((row) => ({
        netbox_field: row.dataset.netboxField,
        netbox_subfield: row.dataset.netboxSubfield,
        inventree_field: row.dataset.inventreeField,
      })),
    );
  };

  /* Grey out InvenTree fields incompatible with the chosen NetBox field,
   * using the server-embedded compatible_netbox_fields lists. */
  const refreshInventreeOptions = () => {
    const netbox = selectedNetbox();
    if (!netbox) return;
    for (const option of inventreeSelect.options) {
      const compatible = option.dataset.compatible.split(',').filter(Boolean);
      const ok = compatible.includes(netbox.value);
      option.disabled = !ok;
      option.title = ok ? '' : `type mismatch: ${netbox.dataset.type} → ${option.dataset.type}`;
    }
  };

  /* Reveal the pointer-attribute select exactly when a pointer field is
   * linked to a string target (validate_mapping raises subfield_required
   * without one — the server enforces what this hints at). */
  const refreshSubfield = () => {
    const netbox = selectedNetbox();
    const inventree = selectedInventree();
    const needed = netbox && inventree &&
      netbox.dataset.type === 'pointer' && inventree.dataset.type === 'string' &&
      netbox.dataset.subfields;
    subfieldContainer.classList.toggle('d-none', !needed);
    if (!needed) {
      subfieldSelect.innerHTML = '';
      return;
    }
    subfieldSelect.innerHTML = netbox.dataset.subfields
      .split(',')
      .filter(Boolean)
      .map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)
      .join('');
  };

  const renderPairRow = (netboxName, subfield, inventreeName) => {
    const netbox = Array.from(netboxSelect.options).find((option) => option.value === netboxName);
    const inventree = Array.from(inventreeSelect.options).find((option) => option.value === inventreeName);
    const netboxType = netbox ? netbox.dataset.type : 'unknown';
    const inventreeType = inventree ? inventree.dataset.type : 'unknown';
    const row = document.createElement('tr');
    row.className = 'pair-row';
    row.dataset.netboxField = netboxName;
    row.dataset.netboxSubfield = subfield;
    row.dataset.inventreeField = inventreeName;
    row.innerHTML = `
      <td>
        <code>${escapeHtml(netboxName)}</code>
        <span class="badge ${netbox ? netbox.dataset.badge : 'text-bg-dark'}">${escapeHtml(netboxType)}</span>
      </td>
      <td>${subfield ? escapeHtml(subfield) : '<span class="text-muted">—</span>'}</td>
      <td class="text-center text-muted">&harr;</td>
      <td>
        <code>${escapeHtml(inventreeName)}</code>
        <span class="badge ${inventree ? inventree.dataset.badge : 'text-bg-dark'}">${escapeHtml(inventreeType)}</span>
      </td>
      <td><span class="badge text-bg-light"></span></td>
      <td class="text-end">
        <button type="button" class="btn btn-sm btn-outline-danger remove-pair">
          <i class="mdi mdi-delete" aria-hidden="true"></i> Remove
        </button>
      </td>`;
    return row;
  };

  const addPair = () => {
    const netbox = selectedNetbox();
    const inventree = selectedInventree();
    if (!netbox || !inventree || inventree.disabled) return;

    const subfieldNeeded = netbox.dataset.type === 'pointer' && inventree.dataset.type === 'string';
    const subfield = subfieldNeeded ? subfieldSelect.value : '';
    if (subfieldNeeded && !subfield) return; // the badge hint says it is required

    const placeholder = pairsBody.querySelector('tr.pair-row-none');
    if (placeholder) placeholder.remove();
    pairsBody.appendChild(renderPairRow(netbox.value, subfield, inventree.value));
    syncPayload();
  };

  /* --- wiring ---------------------------------------------------------------- */

  pairsBody.addEventListener('click', (event) => {
    const button = event.target.closest('.remove-pair');
    if (!button) return;
    button.closest('tr').remove();
    syncPayload();
  });

  netboxSelect.addEventListener('change', () => {
    refreshInventreeOptions();
    refreshSubfield();
  });
  inventreeSelect.addEventListener('change', refreshSubfield);
  if (addButton) addButton.addEventListener('click', addPair);

  form.addEventListener('submit', syncPayload);

  syncPayload();
})();
