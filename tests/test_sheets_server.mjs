/** Apps Script storage exercised with in-memory Google services, never a live account. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import crypto from 'node:crypto';
import vm from 'node:vm';
import test from 'node:test';

const source = readFileSync(new URL('../google-apps-script/Code.gs', import.meta.url), 'utf8');
const OWNER = 'owner@example.com', ADMIN = 7011639945;
const plain = value => JSON.parse(JSON.stringify(value));

function services() {
  const env = {now: Date.parse('2026-11-06T12:00:00.000Z'), id:0, request:0,
    files:new Map(), books:new Map(), props:{}, failCommit:false, lockBusy:false,
    commits:[], requests:[], owner:OWNER, aclCalls:[], failAcl:false};
  const next = prefix => `${prefix}_${String(++env.id).padStart(12, '0')}`;
  const iterator = values => {let i=0;return {hasNext:()=>i<values.length,next:()=>values[i++]};};
  class Item {
    constructor(name, parent=null) {this.id=next('item');this.name=name;this.parent=parent;this.owner=OWNER;this.access='PRIVATE';this.description='';this.permissions=null;this.permissionPages=null;env.files.set(this.id,this);}
    getId(){return this.id;}getName(){return this.name;}getOwner(){return {getEmail:()=>this.owner};}
    getSharingAccess(){return this.access;}getParents(){return iterator(this.parent?[env.files.get(this.parent)]:[]);}
    setDescription(value){this.description=value;return this;}getDescription(){return this.description;}
  }
  class File extends Item {
    constructor(name, bytes=[], parent=null){super(name,parent);this.bytes=Buffer.from(bytes);}
    getBlob(){const bytes=this.bytes;return {getBytes:()=>Array.from(bytes),getDataAsString:()=>bytes.toString('utf8')};}
    makeCopy(name,folder){const result=new File(name,this.bytes,folder.getId());result.description=this.description;if(env.books.has(this.id)){const original=env.books.get(this.id),book=new Book(name,result);book.sheets=original.sheets.map(sheet=>{const copy=new Sheet(sheet.name);copy.data=plain(sheet.data);copy.maxRows=sheet.maxRows;return copy;});env.books.set(result.id,book);}return result;}
  }
  class Folder extends Item {
    createFolder(name){return new Folder(name,this.id);}
    createFile(blob){return new File(blob.name,blob.bytes,this.id);}
    getFiles(){return iterator([...env.files.values()].filter(item=>item instanceof File&&item.parent===this.id));}
    getFilesByName(name){return iterator([...env.files.values()].filter(item=>item instanceof File&&item.parent===this.id&&item.name===name));}
  }
  class Sheet {
    constructor(name){this.name=name;this.id=++env.id;this.data=[];this.maxRows=1000;this.hidden=false;this.protections=[];}
    getSheetId(){return this.id;}getMaxRows(){return this.maxRows;}
    setName(name){this.name=name;return this;}hideSheet(){this.hidden=true;return this;}
    getDataRange(){return {getValues:()=>plain(this.data)};}
    getRange(row,col,rows,columns){return {setValues:values=>{for(let i=0;i<values.length;i++){this.data[row-1+i]??=[];for(let j=0;j<values[i].length;j++)this.data[row-1+i][col-1+j]=values[i][j];}},createFilter:()=>{this.filter=true;}};}
    setFrozenRows(n){this.frozenRows=n;}setFrozenColumns(n){this.frozenColumns=n;}hideColumns(n){}
    getFilter(){return this.filter||null;}
    getProtections(){return this.protections;}
    protect(){const protection={setDescription:()=>protection,setWarningOnly:()=>protection,getEditors:()=>[],removeEditors:()=>{},canDomainEdit:()=>false};this.protections.push(protection);return protection;}
  }
  class Book {
    constructor(name,file=null){this.file=file||new File(name);this.sheets=[];env.books.set(this.file.id,this);}
    getId(){return this.file.id;}getSheets(){return this.sheets;}
    getSheetByName(name){return this.sheets.find(s=>s.name===name)||null;}
    insertSheet(name){const sheet=new Sheet(name);this.sheets.push(sheet);return sheet;}
  }
  const code = {
    Date:class extends Date{constructor(...args){super(...(args.length?args:[env.now]));}static now(){return env.now;}},
    PropertiesService:{getScriptProperties:()=>({getProperties:()=>({...env.props}),setProperties:props=>{Object.assign(env.props,props);}})},
    Session:{getEffectiveUser:()=>({getEmail:()=>env.owner})},
    LockService:{getScriptLock:()=>({waitLock:()=>{if(env.lockBusy)throw Error('locked');},tryLock:()=>!env.lockBusy,releaseLock:()=>{}})},
    Utilities:{getUuid:()=>crypto.randomUUID(),Charset:{UTF_8:'utf8'},DigestAlgorithm:{SHA_256:'sha256'},
      computeDigest:(algorithm,value)=>Array.from(crypto.createHash(algorithm).update(typeof value==='string'?value:Buffer.from(value)).digest()),
      computeHmacSha256Signature:(payload,secret)=>Array.from(crypto.createHmac('sha256',secret).update(payload).digest()),
      base64Encode:bytes=>Buffer.from(bytes).toString('base64'),base64Decode:text=>Array.from(Buffer.from(text,'base64')),
      newBlob:(value,mime,name)=>({bytes:typeof value==='string'?Buffer.from(value):Buffer.from(value),mime,name})},
    DriveApp:{Access:{PRIVATE:'PRIVATE'},createFolder:name=>new Folder(name),getFolderById:id=>{const item=env.files.get(id);if(!(item instanceof Folder))throw Error('private-folder-not-found');return item;},getFileById:id=>{const item=env.files.get(id);if(!(item instanceof File))throw Error('private-file-not-found');return item;}},
    Drive:{Permissions:{list:(id,options)=>{
      env.aclCalls.push({id,options:plain(options)});
      assert.equal(options.pageSize,100);assert.equal(options.supportsAllDrives,true);assert.equal(options.includePermissionsForView,'published');assert.ok(options.fields.includes('emailAddress'));
      if(env.failAcl)throw Error('simulated ACL service error containing private data');
      const item=env.files.get(id);if(!item)throw Error('missing ACL file');
      if(item.permissionPages){const page=item.permissionPages[options.pageToken||''];if(!page)throw Error('invalid ACL continuation');return plain(page);}
      return {permissions:plain(item.permissions??[{id:'owner-permission',type:'user',role:'owner',emailAddress:item.owner}])};
    }}},
    SpreadsheetApp:{ProtectionType:{SHEET:'SHEET'},create:name=>{const book=new Book(name);book.insertSheet('Sheet1');return book;},openById:id=>{if(!env.books.has(id))throw Error('private-sheet-not-found');return env.books.get(id);}},
    Sheets:{Spreadsheets:{batchUpdate:(body,id)=>{
      if(env.failCommit){env.failCommit=false;throw Error('simulated quota failure with secret');}
      const book=env.books.get(id),copies=new Map(book.sheets.map(sheet=>[sheet.id,{data:plain(sheet.data),maxRows:sheet.maxRows}]));
      for(const request of body.requests){env.requests.push(plain(request));if(request.appendDimension){copies.get(request.appendDimension.sheetId).maxRows+=request.appendDimension.length;continue;}
        const {range,rows=[]}=request.updateCells,copy=copies.get(range.sheetId);
        for(let r=range.startRowIndex;r<range.endRowIndex;r++){copy.data[r]??=[];for(let c=range.startColumnIndex;c<range.endColumnIndex;c++)copy.data[r][c]='';}
        rows.forEach((row,i)=>row.values.forEach((cell,j)=>{assert.ok(!cell.userEnteredValue||Object.hasOwn(cell.userEnteredValue,'stringValue'));copy.data[range.startRowIndex+i][range.startColumnIndex+j]=cell.userEnteredValue?.stringValue??'';}));
      }
      for(const sheet of book.sheets){sheet.data=copies.get(sheet.id).data;sheet.maxRows=copies.get(sheet.id).maxRows;while(sheet.data.length>1&&sheet.data.at(-1).every(v=>v===''||v===undefined))sheet.data.pop();}
      env.commits.push(plain(body));return {};
    }}},
    ContentService:{MimeType:{JSON:'application/json'},createTextOutput:text=>({content:text,setMimeType(){return this;}})},
    console:{log:()=>{throw Error('The server must not log secrets or guest data');}}
  };
  env.context=vm.createContext(code);vm.runInContext(source,env.context);
  env.blank = () => {
    const book=new Book('Imported blank template');
    for(const spec of Object.values(env.context.REGISTRY_TABLES)){const sheet=book.insertSheet(spec.name);sheet.data=[Array.from(spec.headers)];}
    return book;
  };
  env.initialize = (backupRoot=null) => {
    const book=env.blank(),photos=new Folder('Private photos'),backups=backupRoot||new Folder('Private backups');
    env.props={REGISTRY_SPREADSHEET_ID:book.getId(),REGISTRY_PHOTO_FOLDER_ID:photos.id,REGISTRY_BACKUP_FOLDER_ID:backups.id,REGISTRY_OWNER_EMAIL:OWNER,REGISTRY_ADMIN_ID:String(ADMIN)};
    env.context.setup();return {book,photos,backups};
  };
  env.bound=env.initialize();
  env.envelope=(action,params={},id=null)=>{
    const payload=JSON.stringify({version:1,operation_id:id||String(++env.request).padStart(32,'0'),issued_at:Math.floor(env.now/1000),action,params});
    return {postData:{contents:JSON.stringify({payload,signature:crypto.createHmac('sha256',env.props.REGISTRY_SECRET).update(payload).digest('hex')})}};
  };
  env.callEnvelope=event=>JSON.parse(env.context.doPost(event).content);
  env.call=(action,params={})=>env.callEnvelope(env.envelope(action,params));
  env.ok=(action,params={})=>{const reply=env.call(action,params);assert.equal(reply.ok,true,JSON.stringify(reply));return reply.result;};
  env.rows=key=>{const state=env.context.registryRead(env.props,false);return plain(state.tables[key]);};
  env.boot=()=>env.ok('bootstrap_employee_access',{initial_ids:[ADMIN],admin_id:ADMIN});
  env.guest=(overrides={})=>env.ok('add_guest',{name:'Иван Петров',phone:'+7 999 123-45-67',comment:'Комментарий',photo:Buffer.from('stored-photo').toString('base64'),photo_file_id:'telegram-file',actor_id:ADMIN,...overrides});
  return env;
}

test('setup binds the selected owner and imported workbook, hides and protects technical sheets',()=>{
  const env=services(),book=env.bound.book,secret=env.props.REGISTRY_SECRET;
  assert.equal(env.context.setup().owner_email,OWNER);assert.equal(env.props.REGISTRY_SECRET,secret);
  assert.equal(env.rows('guests').length,0);assert.equal(env.rows('meta').find(r=>r.key==='admin_id').value,String(ADMIN));
  assert.equal(book.getSheetByName('Операции').hidden,true);assert.equal(book.getSheetByName('Гости').protections.length,1);
  env.owner='another@example.com';assert.throws(()=>env.context.setup(),/только владельцу/);
});

test('private link access still rejects explicit readers, editors, groups, domain and published grants',()=>{
  const extras=[
    {type:'user',role:'reader',emailAddress:'reader@example.com'},
    {type:'user',role:'commenter',emailAddress:'commenter@example.com'},
    {type:'user',role:'writer',emailAddress:'editor@example.com'},
    {type:'group',role:'reader',emailAddress:'team@example.com'},
    {type:'domain',role:'reader',domain:'example.com'},
    {type:'anyone',role:'reader',allowFileDiscovery:false},
    {type:'anyone',role:'publishedReader',view:'published'},
    {type:'user',role:'owner',emailAddress:'other-owner@example.com'},
    {type:'unknown',role:'reader'}
  ];
  for(const extra of extras){
    const env=services(),bookFile=env.files.get(env.bound.book.getId());
    bookFile.permissions=[{type:'user',role:'owner',emailAddress:OWNER},extra];
    assert.equal(bookFile.getSharingAccess(),'PRIVATE');
    const before=plain(env.bound.book.sheets.map(s=>s.data));assert.equal(env.call('health').error.code,'CONFIG',JSON.stringify(extra));
    assert.deepEqual(plain(env.bound.book.sheets.map(s=>s.data)),before);
    assert.throws(()=>env.context.setup(),/закрытый доступ/);
  }
});

test('working and backup folders are checked each RPC, and individual photo sharing is rejected',()=>{
  for(const key of ['REGISTRY_PHOTO_FOLDER_ID','REGISTRY_BACKUP_FOLDER_ID','REGISTRY_BACKUP_ARCHIVE_FOLDER_ID']){
    const env=services();env.files.get(env.props[key]).permissions=[{type:'user',role:'owner',emailAddress:OWNER},{type:'group',role:'writer',emailAddress:'staff@example.com'}];
    assert.equal(env.call('health').error.code,'CONFIG',key);
  }
  const env=services();env.boot();env.guest();const ref=env.ok('snapshot').guests[0].photo_ref;
  env.files.get(ref).permissions=[{type:'user',role:'owner',emailAddress:OWNER},{type:'user',role:'reader',emailAddress:'reader@example.com'}];
  assert.equal(env.call('get_photo',{photo_ref:ref}).error.code,'CONFIG');
});

test('ACL pagination checks later pages and rejects loops, absent owners and service failures',()=>{
  const owner={type:'user',role:'owner',emailAddress:OWNER};
  const env=services(),file=env.files.get(env.bound.book.getId());
  file.permissionPages={'':{permissions:[],nextPageToken:'owner-page'},'owner-page':{permissions:[owner]}};
  assert.equal(env.call('health').ok,true);
  file.permissionPages['owner-page']={permissions:[owner],nextPageToken:'shared-page'};
  file.permissionPages['shared-page']={permissions:[{type:'group',role:'reader',emailAddress:'team@example.com'}]};
  assert.equal(env.call('health').error.code,'CONFIG');
  file.permissionPages={'':{permissions:[owner],nextPageToken:'loop'},loop:{permissions:[],nextPageToken:'loop'}};
  assert.equal(env.call('health').error.code,'CONFIG');
  for(const bad of [{},{permissions:[]},{permissions:[{...owner,emailAddress:undefined}]},{permissions:[{...owner,deleted:true}]},{permissions:[{...owner,pendingOwner:true}]}]){
    file.permissionPages={'':bad};assert.equal(env.call('health').error.code,'CONFIG');
  }
  file.permissionPages=null;env.failAcl=true;const reply=env.call('health');assert.equal(reply.error.code,'CONFIG');assert.ok(!JSON.stringify(reply).includes('private data'));
  env.failAcl=false;assert.equal(env.call('health').ok,true);
});

test('successful ACL results are cached within one RPC and never across requests',()=>{
  const env=services(),id=env.bound.book.getId();env.aclCalls=[];
  env.ok('health');assert.equal(env.aclCalls.filter(c=>c.id===id).length,1);assert.equal(env.aclCalls.length,4);
  env.ok('health');assert.equal(env.aclCalls.filter(c=>c.id===id).length,2);
  env.files.get(id).permissions=[{type:'user',role:'owner',emailAddress:OWNER},{type:'user',role:'reader',emailAddress:'reader@example.com'}];
  assert.equal(env.call('health').error.code,'CONFIG');assert.equal(env.context.REGISTRY_PRIVATE_CACHE,null);
});

test('unsigned, modified and expired requests fail without modifying any table',()=>{
  const env=services(),before=plain(env.bound.book.sheets.map(s=>s.data));
  const request=env.envelope('health');const body=JSON.parse(request.postData.contents);body.signature='0'.repeat(64);request.postData.contents=JSON.stringify(body);
  assert.equal(env.callEnvelope(request).error.code,'AUTH');
  const old=env.envelope('health');env.now+=301000;assert.equal(env.callEnvelope(old).error.code,'AUTH');
  assert.deepEqual(plain(env.bound.book.sheets.map(s=>s.data)),before);assert.equal(JSON.parse(env.context.doGet().content).error.code,'AUTH');
});

test('script locking rejects a busy store without writing',()=>{const env=services();env.lockBusy=true;assert.equal(env.call('health').error.code,'BUSY');assert.equal(env.rows('operations').length,0);});

test('an absent or changed configured owner cannot adopt an existing registry',()=>{
  const env=services();delete env.props.REGISTRY_OWNER_EMAIL;assert.equal(env.call('health').error.code,'CONFIG');
  env.props.REGISTRY_OWNER_EMAIL='another@example.com';env.owner='another@example.com';assert.equal(env.call('health').error.code,'CONFIG');
  assert.throws(()=>env.context.setup(),/владельца|доступ|схема|владельц/);
});

test('setup cannot silently bind a different empty workbook or replace missing configured IDs',()=>{
  const env=services(),original=env.props.REGISTRY_SPREADSHEET_ID;env.props.REGISTRY_SPREADSHEET_ID=env.blank().getId();
  assert.equal(env.call('health').error.code,'CONFIG');assert.throws(()=>env.context.setup(),/смена файлов/);
  delete env.props.REGISTRY_SPREADSHEET_ID;assert.throws(()=>env.context.setup(),/смена файлов/);assert.equal(env.props.REGISTRY_BOUND_SPREADSHEET_ID,original);
});

test('small mutations append a receipt without rewriting the whole operation journal',()=>{
  const env=services();env.boot();const guest=env.guest();for(let i=0;i<6;i++)env.ok('update_guest',{id:guest.id,expected_version:i+1,actor_id:ADMIN,fields:{comment:'Изменение '+i}});
  const operationsId=env.bound.book.getSheetByName('Операции').getSheetId();const changes=env.commits.at(-1).requests.filter(r=>r.updateCells?.range.sheetId===operationsId);
  assert.equal(changes.length,1);assert.equal(changes[0].updateCells.rows.length,1);assert.equal(changes[0].updateCells.range.startRowIndex,8);
});

test('all guest strings are literals and 3000 emoji comments retain Python character limits',()=>{
  const env=services();env.boot();const guest=env.guest({name:'=HYPERLINK("https://invalid")',comment:'😀'.repeat(3000),entry_status:'closed',entry_reason:'Причина'});
  assert.equal(guest.name,'=HYPERLINK("https://invalid")');assert.equal(guest.comment,'😀'.repeat(3000));assert.equal(guest.photo,Buffer.from('stored-photo').toString('base64'));
  assert.ok(env.requests.flatMap(r=>r.updateCells?.rows??[]).flatMap(r=>r.values).every(c=>!c.userEnteredValue||'stringValue'in c.userEnteredValue));
  assert.equal(env.rows('audit_events')[0].changes.entry_status.after,'closed');
});

test('a dropped response retries the exact operation once, even after a later photo edit',()=>{
  const env=services();env.boot();const large=Buffer.alloc(150000,0xab).toString('base64');
  const event=env.envelope('add_guest',{name:'Гость',phone:'+79991234567',comment:'',photo:large,photo_file_id:'old',actor_id:ADMIN});
  const first=env.callEnvelope(event).result;env.ok('update_guest',{id:first.id,expected_version:first.version,actor_id:ADMIN,fields:{photo:Buffer.from('new-photo').toString('base64'),photo_file_id:'new'}});
  const retried=env.callEnvelope(event);assert.equal(retried.result.photo,large);assert.equal(retried.result.version,1);
  assert.equal(env.rows('guests').length,1);assert.equal(env.rows('audit_events').length,2);
  const ledger=env.rows('operations').find(r=>JSON.parse(r.result_json)?.__registry_guest_result&&JSON.parse(r.result_json).guest.version===1);
  assert.ok(ledger.result_json.length<10000);assert.ok(!ledger.result_json.includes(large.slice(0,500)));
});

test('an operation ID cannot be reused for a different signed payload',()=>{
  const env=services();env.boot();const request=env.envelope('add_employee',{user_id:101,actor_id:ADMIN},'a'.repeat(32));assert.equal(env.callEnvelope(request).ok,true);
  const changed=env.envelope('add_employee',{user_id:102,actor_id:ADMIN},'a'.repeat(32));assert.equal(env.callEnvelope(changed).error.code,'AUTH');assert.deepEqual(env.ok('employee_ids'),[101]);
});

test('a Sheets batch failure rolls back card, audit, counters and operation receipt together',()=>{
  const env=services();env.boot();const before=env.rows('meta'),event=env.envelope('add_guest',{name:'Гость',phone:'+79991234567',comment:'',photo:Buffer.from('orphan').toString('base64'),photo_file_id:'photo',actor_id:ADMIN});
  env.failCommit=true;assert.equal(env.callEnvelope(event).error.code,'STORAGE');assert.equal(env.rows('guests').length,0);assert.equal(env.rows('audit_events').length,0);assert.deepEqual(env.rows('meta'),before);
  assert.equal(env.callEnvelope(event).result.id,1);assert.equal(env.rows('guests').length,1);
});


// Independent reference fixtures produced by storage.normalize_name under Python Unicode 15.1.0.
const pythonNames = [{"input":"Stra\u00dfe","query":"STRASSE","stored_name":"Stra\u00dfe","key":"strasse"},{"input":"\u03a3\u039f\u03a3","query":"\u03c3\u03bf\u03c2","stored_name":"\u03a3\u039f\u03a3","key":"\u03c3\u03bf\u03c3"},{"input":"\u13a0\u13e3\u13b3\u13a9","query":"\uab70\uabb3\uab83\uab79","stored_name":"\u13a0\u13e3\u13b3\u13a9","key":"\u13a0\u13e3\u13b3\u13a9"},{"input":"\ufb03","query":"FFI","stored_name":"\ufb03","key":"ffi"},{"input":"\u0130","query":"i\u0307","stored_name":"\u0130","key":"i\u0307"},{"input":" \u001c\u0085\u0401\u001f  \u0418\u0432\u0430\u043d\u2007\u041f\u0415\u0422\u0420\u041e\u0412 ","query":"\u0435 \u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432","stored_name":"\u0401 \u0418\u0432\u0430\u043d \u041f\u0415\u0422\u0420\u041e\u0412","key":"\u0435 \u0438\u0432\u0430\u043d \u043f\u0435\u0442\u0440\u043e\u0432"},{"input":"\ufeff\u0410\u043d\u0434\u0440\u0435\u0439\ufeff","query":"\ufeff\u0430\u043d\u0434\u0440\u0435\u0439\ufeff","stored_name":"\ufeff\u0410\u043d\u0434\u0440\u0435\u0439\ufeff","key":"\ufeff\u0430\u043d\u0434\u0440\u0435\u0439\ufeff"}];

test('phone uniqueness, optimistic versions and photo history survive updates',()=>{
  const env=services();env.boot();const guest=env.guest();
  assert.equal(env.call('add_guest',{name:'Другой',phone:'8(999)1234567',comment:'',photo:'YQ==',photo_file_id:'p',actor_id:ADMIN}).error.code,'DUPLICATE_PHONE');
  const updated=env.ok('update_guest',{id:guest.id,expected_version:1,actor_id:ADMIN,fields:{entry_status:'closed',entry_reason:'Причина'}});
  assert.equal(updated.version,2);assert.equal(env.call('update_guest',{id:guest.id,expected_version:1,actor_id:ADMIN,fields:{name:'Устарело'}}).error.code,'STALE_GUEST');
  assert.equal(env.ok('find_phone',{phone:'89991234567'}).entry_reason,'Причина');
  assert.equal(env.ok('search_guests',{query:'петров',limit:8,offset:0})[0].photo,'');
});

test('Unicode decimal telephone input preserves spelling and matches ASCII throughout card operations',()=>{
  const env=services();env.boot();const arabic='+٨ (٩٩٩) ١٢٣-٤٥-٦٧',fullwidth='+８ (９９９) １２３-４５-６７';
  const guest=env.guest({phone:arabic});assert.equal(guest.phone,arabic);assert.equal(guest.phone_key,'79991234567');
  for(const phone of [arabic,fullwidth,'89991234567'])assert.equal(env.ok('find_phone',{phone}).id,guest.id);
  for(const query of ['١٢٣٤٥','１２３４５','８９９９１２３４５６７']){
    assert.equal(env.ok('search_guests',{query})[0].id,guest.id);assert.equal(env.ok('count_search',{query}),1);
  }
  assert.equal(env.call('add_guest',{name:'Дубль',phone:fullwidth,comment:'',photo:'YQ==',photo_file_id:'p',actor_id:ADMIN}).error.code,'DUPLICATE_PHONE');
  const updated=env.ok('update_guest',{id:guest.id,expected_version:1,actor_id:ADMIN,fields:{phone:fullwidth}});assert.equal(updated.phone,fullwidth);assert.equal(updated.phone_key,'79991234567');
  const mathematical=Array.from('89991234567',n=>String.fromCodePoint(0x1d7ce+Number(n))).join('');
  assert.equal(env.context.registryPhone(mathematical),'79991234567');assert.equal(env.ok('find_phone',{phone:mathematical}).id,guest.id);
  for(const invalid of ['8999²123456','8999Ⅳ123456','8999①123456','8999\ufeff1234567'])assert.equal(env.call('find_phone',{phone:invalid}).error.code,'VALIDATION');
});

test('name search agrees with Python NFKC casefold for German, Greek, Cherokee and whitespace',()=>{
  const env=services();env.boot();
  pythonNames.forEach((fixture,index)=>{
    const guest=env.guest({name:fixture.input,phone:'+79991234'+String(index).padStart(3,'0')});
    assert.equal(guest.name,fixture.stored_name);assert.equal(env.context.registryName(fixture.input),fixture.key);
    assert.equal(env.rows('guests').find(g=>g.id===guest.id).name_key,fixture.key);
    assert.equal(env.ok('search_guests',{query:fixture.query})[0].id,guest.id);assert.equal(env.ok('count_search',{query:fixture.query}),1);
  });
  assert.ok(Object.isFrozen(env.context.REGISTRY_CASEFOLD));assert.ok(Object.isFrozen(env.context.REGISTRY_DECIMAL_BASES));
});

test('Python whitespace includes control separators and excludes BOM in validation and searches',()=>{
  const env=services();env.boot();const whitespace=[9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288];
  for(const point of whitespace){const space=String.fromCodePoint(point);assert.equal(env.context.registryPhone('8'+space+'9991234567'),'79991234567');assert.equal(env.context.registryName(' A'+space+'Ё '),'a е');assert.equal(env.context.registryStrip(space+'x'+space),'x');assert.deepEqual(plain(env.context.registrySearch({tables:{guests:[]}},space)),[]);}
  const longName=env.guest({name:'Иван'+' '.repeat(200)+'Петров',comment:'\u001c\ufeffтекст\ufeff\u0085',entry_reason:'\u0085причина\u001f'});
  assert.equal(longName.name,'Иван Петров');assert.equal(longName.comment,'\ufeffтекст\ufeff');assert.equal(longName.entry_reason,'причина');
  assert.equal(env.context.registryName('\ufeff'),'\ufeff');assert.throws(()=>env.context.registryPhone('8999\ufeff1234567'),/телефон|цифрами/);
});

test('timer state and historical microseconds are preserved exactly',()=>{
  const env=services();env.boot();const g=env.guest();const began='2026-11-06T12:00:00.001999+00:00',ended='2026-11-06T12:00:01.001001+00:00';
  const visit=env.ok('start_visit',{guest_id:g.id,expected_version:1,actor_id:ADMIN,now:began});
  assert.equal(visit.started_at,began);assert.equal(env.call('start_visit',{guest_id:g.id,expected_version:2,actor_id:ADMIN}).error.code,'VISIT_STATE');
  env.ok('stop_visit',{guest_id:g.id,visit_id:visit.id,actor_id:ADMIN,now:ended});assert.equal(env.ok('get_visit_summary',{guest_id:g.id}).completed_seconds,0);
  const current=env.ok('get_guest',{id:g.id});const next=env.ok('start_visit',{guest_id:g.id,expected_version:current.version,actor_id:ADMIN});
  assert.equal(env.call('stop_visit',{guest_id:g.id,visit_id:visit.id,actor_id:ADMIN}).error.code,'VISIT_STATE');assert.equal(env.ok('get_visit_summary',{guest_id:g.id}).active.id,next.id);
});

test('deleted cards remove visits but keep audit and immutable photos',()=>{
  const env=services();env.boot();const guest=env.guest(),snapshot=env.ok('snapshot'),photoRef=snapshot.guests[0].photo_ref;
  env.ok('start_visit',{guest_id:guest.id,expected_version:1,actor_id:ADMIN});const current=env.ok('get_guest',{id:guest.id});env.ok('delete_guest',{id:guest.id,expected_version:current.version,actor_id:ADMIN});
  assert.equal(env.ok('get_guest',{id:guest.id}),null);assert.equal(env.ok('list_visits',{guest_id:guest.id}).length,0);
  assert.equal(env.ok('list_audit',{guest_id:guest.id,limit:5,offset:0})[0].action,'delete');assert.equal(env.ok('get_photo',{photo_ref:photoRef}),guest.photo);
});

test('employees persist but cannot grant access and profiles do not grant it',()=>{
  const env=services();env.boot();assert.equal(env.ok('add_employee',{user_id:101,actor_id:ADMIN}),true);
  assert.equal(env.ok('add_employee',{user_id:101,actor_id:ADMIN}),false);assert.equal(env.call('add_employee',{user_id:102,actor_id:101}).error.code,'PERMISSION');
  env.ok('remember_staff',{actor_id:101,name:'Работник',username:'worker'});assert.equal(env.ok('staff_label',{actor_id:101}),'Работник (@worker)');
  assert.equal(env.ok('has_employee_access',{user_id:102}),false);assert.equal(env.call('bootstrap_employee_access',{initial_ids:[102],admin_id:102}).error.code,'PERMISSION');
  env.ok('bootstrap_employee_access',{initial_ids:[102],admin_id:ADMIN});assert.deepEqual(env.ok('employee_ids'),[101]);
});

test('reminders are exact-visit durable markers and reject stopped sessions',()=>{
  const env=services();env.boot();const guest=env.guest(),at='2026-11-05T12:00:00Z',visit=env.ok('start_visit',{guest_id:guest.id,expected_version:1,actor_id:ADMIN,now:at});
  assert.equal(env.ok('overdue_visits',{threshold_seconds:86400})[0].visit.id,visit.id);
  assert.equal(env.ok('mark_reminder_sent',{visit_id:visit.id,recipient_id:ADMIN}),true);assert.equal(env.ok('mark_reminder_sent',{visit_id:visit.id,recipient_id:ADMIN}),false);
  assert.equal(env.ok('reminder_sent',{visit_id:visit.id,recipient_id:ADMIN}),true);env.ok('stop_visit',{guest_id:guest.id,visit_id:visit.id,actor_id:ADMIN});assert.equal(env.ok('overdue_visits').length,0);
});

test('missing tabs, deleted rows and direct cell changes fail closed instead of recreating data',()=>{
  for(const damage of ['missing','erase','change']){const env=services();env.boot();env.guest();const sheet=env.bound.book.getSheetByName('Гости');
    if(damage==='missing')env.bound.book.sheets=env.bound.book.sheets.filter(s=>s!==sheet);else if(damage==='erase')sheet.data=[sheet.data[0]];else sheet.data[1][1]='Changed outside bot';
    assert.equal(env.call('health').error.code,'SCHEMA');assert.ok(damage!=='missing'||!env.bound.book.getSheetByName('Гости'));
  }
});

test('snapshot pins old immutable images and arbitrary or public Drive files are rejected',()=>{
  const env=services();env.boot();const guest=env.guest(),snapshot=env.ok('snapshot'),old=snapshot.guests[0].photo_ref;
  assert.equal(snapshot.guests[0].photo,'');env.ok('update_guest',{id:guest.id,expected_version:1,actor_id:ADMIN,fields:{photo:'bmV3',photo_file_id:'new'}});assert.equal(env.ok('get_photo',{photo_ref:old}),guest.photo);
  const arbitrary=env.bound.backups.createFile(env.context.Utilities.newBlob('secret','text/plain','secret.txt'));assert.equal(env.call('get_photo',{photo_ref:arbitrary.id}).error.code,'PERMISSION');
  env.files.get(old).access='ANYONE';assert.equal(env.call('get_photo',{photo_ref:old}).error.code,'CONFIG');
});

test('photo payloads accept 10 MiB and reject oversized or damaged bytes safely',()=>{
  const env=services();env.boot();const data=Buffer.alloc(10*1024*1024,4).toString('base64');const staged=env.ok('stage_photo',{admin_id:ADMIN,photo:data});assert.equal(staged.bytes,10*1024*1024);
  assert.equal(env.ok('get_photo',{photo_ref:staged.photo_ref}),data);env.files.get(staged.photo_ref).bytes=Buffer.from('changed');assert.equal(env.call('get_photo',{photo_ref:staged.photo_ref}).error.code,'SCHEMA');
  assert.equal(env.call('stage_photo',{admin_id:ADMIN,photo:Buffer.alloc(10*1024*1024+1).toString('base64')}).error.code,'VALIDATION');
});

test('import preserves all business records, source microseconds and allocation sequences once',()=>{
  const env=services(),at='2026-10-09T10:11:12.345678+00:00';const staged=env.ok('stage_photo',{admin_id:ADMIN,photo:'YWJj'});
  const state={guests:[{id:8,name:'Импорт',phone:'+79991234567',comment:'',photo:'',photo_ref:staged.photo_ref,photo_file_id:'f',created_by:ADMIN,updated_by:101,created_at:at,updated_at:at,version:3,entry_status:'closed',entry_reason:'Причина'}],visits:[{id:4,guest_id:8,started_at:at,stopped_at:null,started_by:101,stopped_by:null}],audit_events:[],staff:[{actor_id:101,name:'Работник',username:'u',updated_at:at}],employee_access:[{user_id:101,added_by:ADMIN,added_at:at}],visit_reminders:[{visit_id:4,recipient_id:101,sent_at:at}]};
  const result=env.ok('import_state',{admin_id:ADMIN,state,sequences:{guests:20,visits:30,audit_events:40}});assert.equal(result.guests,1);assert.equal(env.ok('get_guest',{id:8}).created_at,at);assert.equal(env.ok('employee_ids')[0],101);assert.equal(env.ok('reminder_sent',{visit_id:4,recipient_id:101}),true);
  assert.equal(env.guest({phone:'+79990000001'}).id,21);assert.equal(env.call('import_state',{admin_id:ADMIN,state}).error.code,'CONFLICT');
});

test('backup is daily-idempotent and reuses verified photo archive objects',()=>{
  const env=services();env.boot();env.guest();const first=env.ok('create_backup');const again=env.ok('create_backup');assert.equal(first.manifest_file_id,again.manifest_file_id);
  const archive=env.context.DriveApp.getFolderById(env.props.REGISTRY_BACKUP_ARCHIVE_FOLDER_ID);const files=()=>[...env.files.values()].filter(f=>f.parent===archive.id);assert.equal(files().length,1);
  env.now+=86400000;const next=env.ok('create_backup');assert.notEqual(next.manifest_file_id,first.manifest_file_id);assert.equal(files().length,1);
});

test('missing or corrupted photos cannot produce a successful complete backup',()=>{
  for(const damage of ['missing','corrupt']){const env=services();env.boot();env.guest();const ref=env.ok('snapshot').guests[0].photo_ref;if(damage==='missing')env.files.delete(ref);else env.files.get(ref).bytes=Buffer.from('corrupted');
    const result=env.call('create_backup');assert.equal(result.ok,false);assert.equal(result.error.code,'SCHEMA');assert.equal([...env.files.values()].filter(f=>f.name==='manifest.complete.json').length,0);
  }
});

test('restoration recovers original photos, visits, audit, staff and access after source deletion',()=>{
  const env=services();env.boot();env.ok('add_employee',{user_id:101,actor_id:ADMIN});env.ok('remember_staff',{actor_id:101,name:'Сотрудник',username:'worker'});const guest=env.guest({photo:Buffer.alloc(150000,0x47).toString('base64')});const visit=env.ok('start_visit',{guest_id:guest.id,expected_version:1,actor_id:101,now:'2026-11-05T12:00:00Z'});env.ok('mark_reminder_sent',{visit_id:visit.id,recipient_id:101});
  const expectedAudit=env.ok('list_audit',{limit:100}),backup=env.ok('create_backup'),backupRoot=env.bound.backups;
  const originals=env.context.DriveApp.getFolderById(env.props.REGISTRY_PHOTO_FOLDER_ID).getFiles();while(originals.hasNext())env.files.delete(originals.next().id);env.files.delete(env.props.REGISTRY_SPREADSHEET_ID);env.books.delete(env.props.REGISTRY_SPREADSHEET_ID);
  env.bound=env.initialize(backupRoot);const restored=env.ok('restore_backup',{admin_id:ADMIN,manifest_file_id:backup.manifest_file_id});assert.equal(restored.restored,true);
  assert.equal(env.ok('get_guest',{id:guest.id}).photo,guest.photo);assert.deepEqual(env.ok('list_audit',{limit:100}),expectedAudit);assert.equal(env.ok('get_visit_summary',{guest_id:guest.id}).active.id,visit.id);assert.deepEqual(env.ok('employee_ids'),[101]);assert.equal(env.ok('staff_label',{actor_id:101}),'Сотрудник (@worker)');assert.equal(env.ok('reminder_sent',{visit_id:visit.id,recipient_id:101}),true);
  assert.equal(env.call('restore_backup',{admin_id:ADMIN,manifest_file_id:backup.manifest_file_id}).error.code,'CONFLICT');
});
