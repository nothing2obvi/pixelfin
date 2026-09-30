/* Jellyfin image editor. Edits save immediately to the configured server. */
let imageEditor = null;
function imageEditorLabel(type) { return type === 'Art' ? 'ClearArt' : type === 'BoxRear' ? 'Box Rear' : type; }
const EDITOR_UPLOAD_TYPES = ['Primary', 'Art', 'Backdrop', 'Banner', 'Box', 'BoxRear', 'Disc', 'Logo', 'Menu', 'Thumb'];
function editorIcon(name) {
  const paths = name === 'search' ? '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>' : '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>';
  return `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
}
function editorSearchButton(type, label = `Search ${imageEditorLabel(type)}`) {
  return `<button class="editor-icon-button" onclick="searchEditorImages('${type}')" aria-label="${label}" title="${label}">${editorIcon('search')}</button>`;
}
function editorDeleteButton(type,index) {
  const label = `Delete ${imageEditorLabel(type)}`;
  return `<button class="editor-icon-button editor-delete" onclick="deleteEditorImage('${type}',${index})" aria-label="${label}" title="${label} (Backspace)">${editorIcon('trash')}</button>`;
}
function editorStandardType(type) {
  const images = imageEditor.model.images.filter(i => i.type === type);
  return images.map((image,index)=>imageEditorCard(image,index,false)).join('');
}
async function openImageEditor(libraryId, itemId, trigger) {
  if (imageEditor) return;
  const overlay = document.createElement('div');
  overlay.className = 'modal-backdrop open image-editor-overlay';
  overlay.innerHTML = '<section class="modal image-editor" role="dialog" aria-modal="true" aria-labelledby="imageEditorTitle" tabindex="-1"></section>';
  document.body.append(overlay);
  imageEditor = {libraryId, itemId, trigger, overlay, dialog: overlay.firstElementChild,
    endpoint: `/fresh/api/libraries/${encodeURIComponent(libraryId)}/items/${encodeURIComponent(itemId)}/images`,
    previousOverflow: document.body.style.overflow, model: {types: [], images: []}, busy: false, error: '', changed: false, search: null, preview: null, upload: null};
  document.body.style.overflow = 'hidden';
  overlay.addEventListener('click', event => { if (event.target === overlay) closeImageEditorLevel(); });
  renderImageEditor();
  await reloadImageEditor();
}
async function imageEditorWork(work) {
  const editor = imageEditor;
  if (!editor || editor.busy) return;
  editor.busy = true; editor.error = ''; renderImageEditor();
  try { await work(editor); }
  catch (error) { if (imageEditor === editor) editor.error = error.message; }
  finally { if (imageEditor === editor) { editor.busy = false; renderImageEditor(); } }
}
async function reloadImageEditor() {
  await imageEditorWork(async editor => { editor.model = await api(editor.endpoint); });
}
function closeImageEditorLevel() {
  const editor = imageEditor;
  if (!editor || editor.busy) return;
  if (editor.preview) editor.preview = null;
  else if (editor.upload) { if (editor.upload.url) URL.revokeObjectURL(editor.upload.url); editor.upload = null; }
  else if (editor.search) editor.search = null;
  else {
    document.body.style.overflow = editor.previousOverflow;
    editor.overlay.remove(); imageEditor = null;
    const button = document.querySelector(`#item-${CSS.escape(editor.itemId)} .edit-images-button`);
    (button || editor.trigger)?.focus();
    if (editor.changed) refreshCurrentDetailView().catch(error => showToast(error.message));
    return;
  }
  editor.error = ''; renderImageEditor();
}
function imageEditorCard(image, index, searching) {
  const description = [image.provider, image.width && image.height ? `${image.width} × ${image.height}` : '', image.language].filter(Boolean).join(' · ');
  const controls = searching ? `<button onclick="selectEditorImage(${index})">Select</button>` : `${image.type === 'Backdrop' ? `<button aria-label="Move backdrop left" onclick="moveEditorBackdrop(${image.index},-1)" ${image.index === 0 ? 'disabled' : ''}>←</button><button aria-label="Move backdrop right" onclick="moveEditorBackdrop(${image.index},1)" ${image.index === imageEditor.model.images.filter(i=>i.type==='Backdrop').length-1 ? 'disabled' : ''}>→</button>` : ''}${editorSearchButton(image.type)}${editorDeleteButton(image.type,image.index)}`;
  return `<article class="editor-image-card"><button class="editor-thumbnail" onclick="enlargeEditorImage('${image.type}',${index},${searching})" aria-label="Enlarge ${escapeHtml(imageEditorLabel(image.type))}"><img src="${escapeHtml(image.url)}" alt="${escapeHtml(imageEditorLabel(image.type))}" loading="lazy" referrerpolicy="no-referrer"></button><div class="editor-card-footer">${searching ? '' : `<h3>${escapeHtml(imageEditorLabel(image.type))}</h3>`}<p>${escapeHtml(description)}</p><div class="editor-card-actions">${controls}</div></div></article>`;
}
function renderImageEditor() {
  const editor = imageEditor; if (!editor) return;
  const {model, search, preview, upload} = editor;
  const focused = document.activeElement;
  const focusedId = editor.dialog.contains(focused) ? focused?.id : null;
  const focusedAction = editor.dialog.contains(focused) ? focused?.getAttribute('onclick') : null;
  let body = '';
  if (preview) {
    const images = editorPreviewImages(); const image = images[preview.index];
    body = `<div class="editor-preview"><button onclick="navigateEditorPreview(-1)" aria-label="Previous image" ${images.length<2?'disabled':''}>←</button><img src="${escapeHtml(image.url)}" alt="${escapeHtml(imageEditorLabel(image.type))}" referrerpolicy="no-referrer"><button onclick="navigateEditorPreview(1)" aria-label="Next image" ${images.length<2?'disabled':''}>→</button></div><p>${preview.index+1} / ${images.length} · ${escapeHtml(image.provider || 'Jellyfin')} · ${escapeHtml(image.width || '?')} × ${escapeHtml(image.height || '?')}</p>${preview.searching ? `<button onclick="selectEditorImage(${preview.index})">Select (Enter)</button>` : `${editorDeleteButton(image.type,image.index)}`}`;
  } else if (upload) {
    body = `<h3>Upload image</h3><div class="editor-dropzone" id="editorDropzone"><input id="editorUploadFile" class="editor-file-input" type="file" accept="image/png,image/jpeg,image/webp,image/gif" onchange="chooseEditorUpload(this.files[0])" tabindex="-1"><button class="editor-choose-file" onclick="document.getElementById('editorUploadFile').click()">Choose file</button><span>Drop an image here or choose a file</span>${upload.url ? `<img src="${escapeHtml(upload.url)}" alt="Upload preview"><span>${escapeHtml(upload.file.name)}</span>` : ''}</div><label for="editorUploadType">Image type</label> <select id="editorUploadType" onchange="imageEditor.upload.type=this.value;renderImageEditor()">${EDITOR_UPLOAD_TYPES.filter(type=>model.types.includes(type)).map(type=>`<option value="${type}" ${type===upload.type?'selected':''}>${escapeHtml(imageEditorLabel(type))}</option>`).join('')}</select> <button onclick="saveEditorUpload()" ${upload.file?'':'disabled'}>${editorUploadAction(upload.type)}</button>`;

  } else if (search) {
    body = `<h3>Search</h3><div class="editor-search-controls"><label>Source<select id="editorSearchSource" onchange="searchEditorImages(imageEditor.search.type,false,imageEditor.search.allLanguages,this.value)"><option value="">All</option>${(search.providers || []).map(provider=>`<option value="${escapeHtml(provider)}" ${provider===search.provider?'selected':''}>${escapeHtml(provider)}</option>`).join('')}</select></label><label>Type<select id="editorSearchType" onchange="searchEditorImages(this.value,false,imageEditor.search.allLanguages,imageEditor.search.provider)">${EDITOR_UPLOAD_TYPES.filter(type=>model.types.includes(type)).map(type=>`<option value="${type}" ${type===search.type?'selected':''}>${escapeHtml(imageEditorLabel(type))}</option>`).join('')}</select></label><span class="editor-search-range">${search.images.length ? search.start+1 : 0}–${search.start+search.images.length} of ${search.total}</span><button class="editor-icon-button" aria-label="Previous search page" title="Previous page" onclick="searchEditorPage(-1)" ${search.start===0?'disabled':''}>←</button><button class="editor-icon-button" aria-label="Next search page" title="Next page" onclick="searchEditorPage(1)" ${search.start+30>=search.total?'disabled':''}>→</button><label class="editor-all-languages"><input type="checkbox" id="editorAllLanguages" ${search.allLanguages?'checked':''} onchange="searchEditorImages(imageEditor.search.type,false,this.checked,imageEditor.search.provider)"> All languages</label></div><div class="editor-grid editor-search-results editor-search-${search.type.toLowerCase()}">${search.images.map((i,index)=>imageEditorCard(i,index,true)).join('')}</div>${!search.images.length&&!editor.busy?'<p>No images found.</p>':''}`;
  } else {
    const standardTypes = [...new Set([...model.images.map(image=>image.type), ...EDITOR_UPLOAD_TYPES])].filter(type => type !== 'Backdrop' && EDITOR_UPLOAD_TYPES.includes(type) && model.types.includes(type));
    body = `<div class="editor-section-heading"><h3>Images</h3>${editorSearchButton('Primary', 'Search images')}<button class="editor-icon-button editor-add" onclick="openEditorUpload()" aria-label="Upload image" title="Upload image">+</button></div><div class="editor-standard-grid">${standardTypes.map(editorStandardType).join('')}</div><section class="editor-type editor-backdrops"><h3>Backdrops ${editorSearchButton('Backdrop')}</h3><div class="editor-grid">${model.images.filter(i=>i.type==='Backdrop').sort((a,b)=>a.index-b.index).map((i,index)=>imageEditorCard(i,index,false)).join('')}</div></section>`;
  }
  editor.dialog.innerHTML = `<header><h2 id="imageEditorTitle">Edit Images${model.name ? ` — ${escapeHtml(model.name)}` : ''}</h2><button onclick="closeImageEditorLevel()">${preview||search||upload?'Back':'Close'}</button></header><div class="modal-body"><p class="subtle">Edits are immediately saved to the Jellyfin server and to Pixelfin.</p><p role="alert" class="editor-error">${escapeHtml(editor.error)}</p>${editor.busy?'<p role="status">Working…</p>':''}${body}</div>`;
  editor.dialog.querySelectorAll('button,input,select').forEach(element => { if (editor.busy) element.disabled = true; });
  const dropzone = editor.dialog.querySelector('#editorDropzone');
  if (dropzone) {
    dropzone.addEventListener('dragover', event => { event.preventDefault(); dropzone.classList.add('dragging'); });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragging'));
    dropzone.addEventListener('drop', event => { event.preventDefault(); if (!editor.busy) chooseEditorUpload(event.dataTransfer.files[0]); });
  }
  const restore = focusedAction && [...editor.dialog.querySelectorAll('[onclick]')].find(element=>element.getAttribute('onclick')===focusedAction&&!element.disabled);
  (restore || (focusedId && editor.dialog.querySelector(`#${CSS.escape(focusedId)}`)) || editor.dialog).focus({preventScroll:true});
}
function editorPreviewImages() {
  const editor = imageEditor;
  return editor.preview.searching ? editor.search.images : editor.model.images.filter(i=>i.type===editor.preview.type);
}
function enlargeEditorImage(type,index,searching) { imageEditor.preview={type,index,searching}; renderImageEditor(); }
function navigateEditorPreview(delta) {
  if (!imageEditor?.preview || imageEditor.busy) return;
  const count = editorPreviewImages().length;
  imageEditor.preview.index=(imageEditor.preview.index+delta+count)%count; renderImageEditor();
}
async function searchEditorImages(type,more=false,allLanguages=imageEditor?.search?.allLanguages ?? false,provider='',start=0) {
  await imageEditorWork(async editor => {
    editor.search={type,images:[],total:0,allLanguages,provider,start,providers:editor.search?.providers || []};
    editor.dialog.scrollTop=0;
    renderImageEditor();
    const data = await api(`${editor.endpoint}/search?${new URLSearchParams({type,start:String(editor.search.start),allLanguages:String(editor.search.allLanguages),provider:editor.search.provider})}`);
    editor.search.images.push(...data.images); editor.search.total=data.total; editor.search.providers=data.providers || [];
  });
}
function searchEditorPage(delta) {
  const search = imageEditor?.search;
  if (!search || imageEditor.busy) return;
  const start = search.start + delta*30;
  if (start < 0 || start >= search.total) return;
  return searchEditorImages(search.type,false,search.allLanguages,search.provider,start);
}
function editorUploadAction(type) {
  return type === 'Backdrop' || !imageEditor.model.images.some(image => image.type === type) ? 'Add' : 'Replace';
}
function openEditorUpload() { imageEditor.upload={type:'Primary',file:null,url:null}; renderImageEditor(); }
function chooseEditorUpload(file) {
  if (!file || !imageEditor?.upload) return;
  const editor = imageEditor;
  if (file.size > 10*1024*1024) { editor.error='Upload must be at most 10 MB.'; renderImageEditor(); return; }
  if (!['image/png','image/jpeg','image/webp','image/gif'].includes(file.type)) { editor.error='Use PNG, JPEG, WebP, or GIF.'; renderImageEditor(); return; }
  if (editor.upload.url) URL.revokeObjectURL(editor.upload.url);
  editor.upload.file=file; editor.upload.url=URL.createObjectURL(file); editor.error=''; renderImageEditor();
}
function saveEditorUpload() { if (imageEditor?.upload?.file) return uploadEditorImage(imageEditor.upload.file); }
async function mutateEditorImage(body) {
  await imageEditorWork(async editor => {
    const result = await api(editor.endpoint, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,revision:editor.model.revision})});
    editor.changed=true;
    if (result.images) editor.model={...editor.model,...result};
    else editor.error=result.warning || 'Image saved; reload images to continue.';
    editor.preview=null; if (editor.upload?.url) URL.revokeObjectURL(editor.upload.url); editor.upload=null;
    if (body.action==='select' && body.type!=='Backdrop') editor.search=null;
    if (currentDetailMode !== 'library' || hideCompleteItems) pinnedTaskItemIds.add(editor.itemId);
    if (result.job_id) pollScanJob(result.job_id,{kind:'item',libraryId:editor.libraryId,itemId:editor.itemId});
    if (result.warning) showToast(result.warning);
  });
}
function selectEditorImage(index) { const image=imageEditor.search.images[index]; return mutateEditorImage({action:'select',type:image.type,candidate:image.candidate}); }
function deleteEditorImage(type,index) { return mutateEditorImage({action:'delete',type,index}); }
function moveEditorBackdrop(index,delta) { return mutateEditorImage({action:'move',type:'Backdrop',index,newIndex:index+delta}); }
async function uploadEditorImage(file) {
  if (!file) return;
  if (file.size>10*1024*1024) {imageEditor.error='Upload must be at most 10 MB.';renderImageEditor();return;}
  const editor=imageEditor, type=editor.upload.type;
  try {
    const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('Unable to read image.'));reader.readAsDataURL(file);});
    if(imageEditor===editor) await mutateEditorImage({action:'upload',type,data});
  } catch(error) {if(imageEditor===editor){editor.error=error.message;renderImageEditor();}}
}
document.addEventListener('keydown',event=>{
  const editor=imageEditor;if(!editor)return;
  if(event.key==='Escape') {event.preventDefault();event.stopImmediatePropagation();closeImageEditorLevel();return;}
  const input=['INPUT','TEXTAREA','SELECT'].includes(event.target.tagName);
  if(editor.preview&&!input&&['ArrowLeft','ArrowRight','Enter','Backspace'].includes(event.key)) {
    event.preventDefault();event.stopImmediatePropagation();if(editor.busy||event.repeat)return;
    if(event.key==='ArrowLeft'||event.key==='ArrowRight')navigateEditorPreview(event.key==='ArrowLeft'?-1:1);
    else if(event.key==='Enter'&&editor.preview.searching)selectEditorImage(editor.preview.index);
    else if(event.key==='Backspace'&&!editor.preview.searching){const i=editorPreviewImages()[editor.preview.index];deleteEditorImage(i.type,i.index);}
  }
  if(event.key==='Tab') {
    const nodes=[...editor.dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled)')];
    const first=nodes[0],last=nodes[nodes.length-1];
    if(!first){event.preventDefault();return;}
    if(event.shiftKey&&(document.activeElement===first||document.activeElement===editor.dialog)){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===editor.dialog)){event.preventDefault();first.focus();}
  }
},true);
