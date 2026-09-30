const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function editorContext() {
  let keyHandler;
  const context = vm.createContext({document:{addEventListener:(name,fn)=>keyHandler=fn,
    querySelector:()=>null},CSS:{escape:x=>x}, currentDetailMode:'library',hideCompleteItems:false,
    pinnedTaskItemIds:new Set(), refreshCurrentDetailView:async()=>{},showToast:()=>{}});
  vm.runInContext(fs.readFileSync('assets/image-editor.js','utf8'),context);
  vm.runInContext(`renderImageEditor=()=>{};
    imageEditor={busy:false,model:{images:[{type:'Primary',index:0},{type:'Backdrop',index:0},{type:'Backdrop',index:1}]},
    search:{images:[{type:'Primary',candidate:'one'},{type:'Primary',candidate:'two'}]},
    preview:{type:'Backdrop',index:0,searching:false},dialog:{},error:''};
    calls=[]; mutateEditorImage=body=>calls.push(body);`,context);
  return {context,run:source=>vm.runInContext(source,context),key:key=>keyHandler({key,target:{tagName:'DIV'},preventDefault(){},stopImmediatePropagation(){}})};
}
test('arrow keys navigate only images of the previewed type and wrap',()=>{
  const e=editorContext();e.key('ArrowRight');assert.equal(e.run('imageEditor.preview.index'),1);
  e.key('ArrowRight');assert.equal(e.run('imageEditor.preview.index'),0);
  e.key('ArrowLeft');assert.equal(e.run('imageEditor.preview.index'),1);
});
test('Enter selects remote results and Backspace deletes only existing artwork',()=>{
  const e=editorContext();e.key('Enter');assert.equal(e.run('calls.length'),0);
  e.key('Backspace');assert.equal(e.run('calls[0].action'),'delete');
  e.run('calls=[];imageEditor.preview={type:"Primary",index:1,searching:true}');
  e.key('Backspace');assert.equal(e.run('calls.length'),0);
  e.key('Enter');assert.equal(e.run('calls[0].candidate'),'two');
});
test('busy edits ignore mutation shortcuts',()=>{
  const e=editorContext();e.run('imageEditor.busy=true');e.key('Backspace');assert.equal(e.run('calls.length'),0);
});
test('Escape returns one level at a time',()=>{
  const e=editorContext();e.key('Escape');assert.equal(e.run('imageEditor.preview'),null);
  assert.equal(e.run('imageEditor.search.images.length'),2);
  e.key('Escape');assert.equal(e.run('imageEditor.search'),null);
});
test('single upload stages a file and selected type until the user submits',()=>{
  const e=editorContext();e.context.URL={createObjectURL:()=> 'blob:preview',revokeObjectURL:()=>{}};
  e.run('openEditorUpload();chooseEditorUpload({name:"poster.png",type:"image/png",size:10})');
  assert.equal(e.run('imageEditor.upload.type'),'Primary');assert.equal(e.run('calls.length'),0);
  e.run('imageEditor.upload.type="Backdrop";uploadEditorImage=file=>calls.push({file,type:imageEditor.upload.type});saveEditorUpload()');
  assert.equal(e.run('calls[0].type'),'Backdrop');assert.equal(e.run('calls[0].file.name'),'poster.png');
});
test('invalid uploads stay staged without a write',()=>{
  const e=editorContext();e.run('openEditorUpload();chooseEditorUpload({type:"text/plain",size:10})');
  assert.equal(e.run('imageEditor.upload.file'),null);assert.equal(e.run('calls.length'),0);
  assert.match(e.run('imageEditor.error'),/PNG/);
});
test('search pages retain filters and changing type resets pagination',async()=>{
  const e=editorContext(),queries=[];
  e.context.URLSearchParams=URLSearchParams;
  e.context.api=async url=>{queries.push(new URL(url,'http://test.invalid').searchParams);return {images:[],total:86,providers:['Example']};};
  e.run('imageEditor.endpoint="/images";imageEditor.dialog={scrollTop:0};imageEditor.search=null');
  await e.run('searchEditorImages("Primary",false,true,"Example")');
  await e.run('searchEditorPage(1)');
  assert.equal(queries[1].get('start'),'30');assert.equal(queries[1].get('provider'),'Example');
  assert.equal(queries[1].get('allLanguages'),'true');
  await e.run('searchEditorImages("Logo",false,imageEditor.search.allLanguages,imageEditor.search.provider)');
  assert.equal(queries[2].get('start'),'0');assert.equal(queries[2].get('type'),'Logo');
});
test('upload action reflects whether the selected image type already exists',()=>{
  const e=editorContext();
  assert.equal(e.run('editorUploadAction("Primary")'),'Replace');
  assert.equal(e.run('editorUploadAction("Logo")'),'Add');
  assert.equal(e.run('editorUploadAction("Backdrop")'),'Add');
});
