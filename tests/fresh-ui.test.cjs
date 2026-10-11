const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('templates/fresh.html','utf8');
function load(context,names) {
 for(const name of names) {
  const start=source.indexOf(`function ${name}(`);
  const prefix=source.slice(start-6,start)==='async '?'async ':'';
  vm.runInContext(prefix+source.slice(start,source.indexOf('\n}',start)+2),context);
 }
}
test('obsolete season-filter preference no longer excludes missing season posters',()=>{
 const context=vm.createContext({localStorage:{getItem:key=>key==='pixelfin_ignore_missing_seasons'?'1':'0'},imageSelectedForItem:()=>true});
 load(context,['getIssueFilters','imageVisibleByIssueFilter','listedIssueImages']);
 const result=vm.runInContext(`listedIssueImages({images:[{code:'sp',status:'missing'},{code:'p',status:'missing'}]})`,context);
 assert.equal(result.missing.length,2);
 assert.equal(result.missing[1].code,'sp');
});
test('image-edit scan completion refreshes without scheduling a reload after the editor closes',async()=>{
 let reloads=0,options;
 const context=vm.createContext({document:{getElementById:()=>null},api:async()=>({job:{state:'done'}}),updateGlobalScanStatus:async()=>{},updateIssueCounts:async()=>{},
 showToast:()=>{},detailIsScrollTarget:()=>true,refreshCurrentDetailView:async value=>options=value,scheduleScanCompletionReload:()=>reloads++});
 load(context,['pollScanJob']);
 await vm.runInContext('pollScanJob("job",{kind:"item",imageEdit:true})',context);
 assert.equal(reloads,0);assert.equal(options.preserveScrollPosition,true);
 await vm.runInContext('pollScanJob("job",{kind:"item"})',context);
 assert.equal(reloads,1);
});
test('listing refresh restores the current offset rather than snapping to an item heading',async()=>{
 let restored;
 const window={scrollY:750,scrollTo:value=>{restored=value}};
 const context=vm.createContext({window,document:{getElementById:()=>({classList:{contains:()=>true}})},
 currentDetailMode:'library',currentLibraryId:'lib',hideCompleteItems:false,currentLibraryPayload:null,
 currentVisibleEntryId:()=>{throw Error('Must not snap to an item')},requestAnimationFrame:callback=>callback(),
 api:async()=>{window.scrollY=800;return {library:{},items:[]}},renderDetail:()=>{window.scrollY=0}});
 load(context,['refreshCurrentDetailView']);
 await vm.runInContext('refreshCurrentDetailView({preserveScrollPosition:true})',context);
 assert.equal(restored.top,800);assert.equal(restored.behavior,'instant');
});
