/** PC Arena storage. Explicit setup; signed RPC; atomic writes and durable retries.
 * The web-app URL is not authorization: every POST requires a valid HMAC.
 * All rows are written as literal strings; user text never becomes a formula.
 */
function registryOwner() {var value=PropertiesService.getScriptProperties().getProperties().REGISTRY_OWNER_EMAIL;registryAssert(typeof value==='string'&&/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)&&value===value.trim().toLowerCase(),'CONFIG','Задайте адрес владельца в настройках скрипта.');return value;}
var REGISTRY_ADMIN = 7011639945;
var REGISTRY_SCHEMA = 1;
var REGISTRY_MAX_PHOTO = 10 * 1024 * 1024;
// Successful ACL checks live only inside one setup/RPC execution.
var REGISTRY_PRIVATE_CACHE = null;
// Generated from Python Unicode database 15.1.0 (Python 3.13): casefold/lower differences and all decimal blocks.
var REGISTRY_CASEFOLD = Object.freeze({"\u00b5":"\u03bc","\u00df":"ss","\u0149":"\u02bcn","\u017f":"s","\u01f0":"j\u030c","\u0345":"\u03b9","\u0390":"\u03b9\u0308\u0301","\u03b0":"\u03c5\u0308\u0301","\u03c2":"\u03c3","\u03d0":"\u03b2","\u03d1":"\u03b8","\u03d5":"\u03c6","\u03d6":"\u03c0","\u03f0":"\u03ba","\u03f1":"\u03c1","\u03f5":"\u03b5","\u0587":"\u0565\u0582","\u13a0":"\u13a0","\u13a1":"\u13a1","\u13a2":"\u13a2","\u13a3":"\u13a3","\u13a4":"\u13a4","\u13a5":"\u13a5","\u13a6":"\u13a6","\u13a7":"\u13a7","\u13a8":"\u13a8","\u13a9":"\u13a9","\u13aa":"\u13aa","\u13ab":"\u13ab","\u13ac":"\u13ac","\u13ad":"\u13ad","\u13ae":"\u13ae","\u13af":"\u13af","\u13b0":"\u13b0","\u13b1":"\u13b1","\u13b2":"\u13b2","\u13b3":"\u13b3","\u13b4":"\u13b4","\u13b5":"\u13b5","\u13b6":"\u13b6","\u13b7":"\u13b7","\u13b8":"\u13b8","\u13b9":"\u13b9","\u13ba":"\u13ba","\u13bb":"\u13bb","\u13bc":"\u13bc","\u13bd":"\u13bd","\u13be":"\u13be","\u13bf":"\u13bf","\u13c0":"\u13c0","\u13c1":"\u13c1","\u13c2":"\u13c2","\u13c3":"\u13c3","\u13c4":"\u13c4","\u13c5":"\u13c5","\u13c6":"\u13c6","\u13c7":"\u13c7","\u13c8":"\u13c8","\u13c9":"\u13c9","\u13ca":"\u13ca","\u13cb":"\u13cb","\u13cc":"\u13cc","\u13cd":"\u13cd","\u13ce":"\u13ce","\u13cf":"\u13cf","\u13d0":"\u13d0","\u13d1":"\u13d1","\u13d2":"\u13d2","\u13d3":"\u13d3","\u13d4":"\u13d4","\u13d5":"\u13d5","\u13d6":"\u13d6","\u13d7":"\u13d7","\u13d8":"\u13d8","\u13d9":"\u13d9","\u13da":"\u13da","\u13db":"\u13db","\u13dc":"\u13dc","\u13dd":"\u13dd","\u13de":"\u13de","\u13df":"\u13df","\u13e0":"\u13e0","\u13e1":"\u13e1","\u13e2":"\u13e2","\u13e3":"\u13e3","\u13e4":"\u13e4","\u13e5":"\u13e5","\u13e6":"\u13e6","\u13e7":"\u13e7","\u13e8":"\u13e8","\u13e9":"\u13e9","\u13ea":"\u13ea","\u13eb":"\u13eb","\u13ec":"\u13ec","\u13ed":"\u13ed","\u13ee":"\u13ee","\u13ef":"\u13ef","\u13f0":"\u13f0","\u13f1":"\u13f1","\u13f2":"\u13f2","\u13f3":"\u13f3","\u13f4":"\u13f4","\u13f5":"\u13f5","\u13f8":"\u13f0","\u13f9":"\u13f1","\u13fa":"\u13f2","\u13fb":"\u13f3","\u13fc":"\u13f4","\u13fd":"\u13f5","\u1c80":"\u0432","\u1c81":"\u0434","\u1c82":"\u043e","\u1c83":"\u0441","\u1c84":"\u0442","\u1c85":"\u0442","\u1c86":"\u044a","\u1c87":"\u0463","\u1c88":"\ua64b","\u1e96":"h\u0331","\u1e97":"t\u0308","\u1e98":"w\u030a","\u1e99":"y\u030a","\u1e9a":"a\u02be","\u1e9b":"\u1e61","\u1e9e":"ss","\u1f50":"\u03c5\u0313","\u1f52":"\u03c5\u0313\u0300","\u1f54":"\u03c5\u0313\u0301","\u1f56":"\u03c5\u0313\u0342","\u1f80":"\u1f00\u03b9","\u1f81":"\u1f01\u03b9","\u1f82":"\u1f02\u03b9","\u1f83":"\u1f03\u03b9","\u1f84":"\u1f04\u03b9","\u1f85":"\u1f05\u03b9","\u1f86":"\u1f06\u03b9","\u1f87":"\u1f07\u03b9","\u1f88":"\u1f00\u03b9","\u1f89":"\u1f01\u03b9","\u1f8a":"\u1f02\u03b9","\u1f8b":"\u1f03\u03b9","\u1f8c":"\u1f04\u03b9","\u1f8d":"\u1f05\u03b9","\u1f8e":"\u1f06\u03b9","\u1f8f":"\u1f07\u03b9","\u1f90":"\u1f20\u03b9","\u1f91":"\u1f21\u03b9","\u1f92":"\u1f22\u03b9","\u1f93":"\u1f23\u03b9","\u1f94":"\u1f24\u03b9","\u1f95":"\u1f25\u03b9","\u1f96":"\u1f26\u03b9","\u1f97":"\u1f27\u03b9","\u1f98":"\u1f20\u03b9","\u1f99":"\u1f21\u03b9","\u1f9a":"\u1f22\u03b9","\u1f9b":"\u1f23\u03b9","\u1f9c":"\u1f24\u03b9","\u1f9d":"\u1f25\u03b9","\u1f9e":"\u1f26\u03b9","\u1f9f":"\u1f27\u03b9","\u1fa0":"\u1f60\u03b9","\u1fa1":"\u1f61\u03b9","\u1fa2":"\u1f62\u03b9","\u1fa3":"\u1f63\u03b9","\u1fa4":"\u1f64\u03b9","\u1fa5":"\u1f65\u03b9","\u1fa6":"\u1f66\u03b9","\u1fa7":"\u1f67\u03b9","\u1fa8":"\u1f60\u03b9","\u1fa9":"\u1f61\u03b9","\u1faa":"\u1f62\u03b9","\u1fab":"\u1f63\u03b9","\u1fac":"\u1f64\u03b9","\u1fad":"\u1f65\u03b9","\u1fae":"\u1f66\u03b9","\u1faf":"\u1f67\u03b9","\u1fb2":"\u1f70\u03b9","\u1fb3":"\u03b1\u03b9","\u1fb4":"\u03ac\u03b9","\u1fb6":"\u03b1\u0342","\u1fb7":"\u03b1\u0342\u03b9","\u1fbc":"\u03b1\u03b9","\u1fbe":"\u03b9","\u1fc2":"\u1f74\u03b9","\u1fc3":"\u03b7\u03b9","\u1fc4":"\u03ae\u03b9","\u1fc6":"\u03b7\u0342","\u1fc7":"\u03b7\u0342\u03b9","\u1fcc":"\u03b7\u03b9","\u1fd2":"\u03b9\u0308\u0300","\u1fd3":"\u03b9\u0308\u0301","\u1fd6":"\u03b9\u0342","\u1fd7":"\u03b9\u0308\u0342","\u1fe2":"\u03c5\u0308\u0300","\u1fe3":"\u03c5\u0308\u0301","\u1fe4":"\u03c1\u0313","\u1fe6":"\u03c5\u0342","\u1fe7":"\u03c5\u0308\u0342","\u1ff2":"\u1f7c\u03b9","\u1ff3":"\u03c9\u03b9","\u1ff4":"\u03ce\u03b9","\u1ff6":"\u03c9\u0342","\u1ff7":"\u03c9\u0342\u03b9","\u1ffc":"\u03c9\u03b9","\uab70":"\u13a0","\uab71":"\u13a1","\uab72":"\u13a2","\uab73":"\u13a3","\uab74":"\u13a4","\uab75":"\u13a5","\uab76":"\u13a6","\uab77":"\u13a7","\uab78":"\u13a8","\uab79":"\u13a9","\uab7a":"\u13aa","\uab7b":"\u13ab","\uab7c":"\u13ac","\uab7d":"\u13ad","\uab7e":"\u13ae","\uab7f":"\u13af","\uab80":"\u13b0","\uab81":"\u13b1","\uab82":"\u13b2","\uab83":"\u13b3","\uab84":"\u13b4","\uab85":"\u13b5","\uab86":"\u13b6","\uab87":"\u13b7","\uab88":"\u13b8","\uab89":"\u13b9","\uab8a":"\u13ba","\uab8b":"\u13bb","\uab8c":"\u13bc","\uab8d":"\u13bd","\uab8e":"\u13be","\uab8f":"\u13bf","\uab90":"\u13c0","\uab91":"\u13c1","\uab92":"\u13c2","\uab93":"\u13c3","\uab94":"\u13c4","\uab95":"\u13c5","\uab96":"\u13c6","\uab97":"\u13c7","\uab98":"\u13c8","\uab99":"\u13c9","\uab9a":"\u13ca","\uab9b":"\u13cb","\uab9c":"\u13cc","\uab9d":"\u13cd","\uab9e":"\u13ce","\uab9f":"\u13cf","\uaba0":"\u13d0","\uaba1":"\u13d1","\uaba2":"\u13d2","\uaba3":"\u13d3","\uaba4":"\u13d4","\uaba5":"\u13d5","\uaba6":"\u13d6","\uaba7":"\u13d7","\uaba8":"\u13d8","\uaba9":"\u13d9","\uabaa":"\u13da","\uabab":"\u13db","\uabac":"\u13dc","\uabad":"\u13dd","\uabae":"\u13de","\uabaf":"\u13df","\uabb0":"\u13e0","\uabb1":"\u13e1","\uabb2":"\u13e2","\uabb3":"\u13e3","\uabb4":"\u13e4","\uabb5":"\u13e5","\uabb6":"\u13e6","\uabb7":"\u13e7","\uabb8":"\u13e8","\uabb9":"\u13e9","\uabba":"\u13ea","\uabbb":"\u13eb","\uabbc":"\u13ec","\uabbd":"\u13ed","\uabbe":"\u13ee","\uabbf":"\u13ef","\ufb00":"ff","\ufb01":"fi","\ufb02":"fl","\ufb03":"ffi","\ufb04":"ffl","\ufb05":"st","\ufb06":"st","\ufb13":"\u0574\u0576","\ufb14":"\u0574\u0565","\ufb15":"\u0574\u056b","\ufb16":"\u057e\u0576","\ufb17":"\u0574\u056d"});
var REGISTRY_DECIMAL_BASES = Object.freeze([0x30,0x660,0x6f0,0x7c0,0x966,0x9e6,0xa66,0xae6,0xb66,0xbe6,0xc66,0xce6,0xd66,0xde6,0xe50,0xed0,0xf20,0x1040,0x1090,0x17e0,0x1810,0x1946,0x19d0,0x1a80,0x1a90,0x1b50,0x1bb0,0x1c40,0x1c50,0xa620,0xa8d0,0xa900,0xa9d0,0xa9f0,0xaa50,0xabf0,0xff10,0x104a0,0x10d30,0x11066,0x110f0,0x11136,0x111d0,0x112f0,0x11450,0x114d0,0x11650,0x116c0,0x11730,0x118e0,0x11950,0x11c50,0x11d50,0x11da0,0x11f50,0x16a60,0x16ac0,0x16b50,0x1d7ce,0x1d7d8,0x1d7e2,0x1d7ec,0x1d7f6,0x1e140,0x1e2f0,0x1e4f0,0x1e950,0x1fbf0]);
var REGISTRY_PY_SPACE = /[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/;
var REGISTRY_PY_STRIP = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g;
var REGISTRY_TABLES = {
  meta: {name:'Настройки', fields:['key','value'], headers:['Ключ','Значение']},
  guests: {name:'Гости', fields:['id','name','name_key','phone','phone_key','comment','photo_drive_id','photo_file_id','created_by','updated_by','created_at','updated_at','version','entry_status','entry_reason'], headers:['ID','Имя','Нормализованное имя','Телефон','Нормализованный телефон','Комментарий','ID фото на Диске','Фото Telegram','Создал (Telegram ID)','Изменил (Telegram ID)','Создано (UTC)','Изменено (UTC)','Версия','Статус входа','Причина']},
  visits: {name:'Посещения', fields:['id','guest_id','started_at','stopped_at','started_by','stopped_by'], headers:['ID','ID гостя','Начало (UTC)','Окончание (UTC)','Начал (Telegram ID)','Остановил (Telegram ID)']},
  audit_events: {name:'История', fields:['id','guest_id','guest_name','actor_id','action','changes','created_at'], headers:['ID','ID гостя','Имя гостя','Сотрудник (Telegram ID)','Действие','Изменения (JSON)','Время (UTC)']},
  staff: {name:'Сотрудники', fields:['actor_id','name','username','updated_at'], headers:['Telegram ID','Имя','Username','Обновлено (UTC)']},
  employee_access: {name:'Доступ', fields:['user_id','added_by','added_at'], headers:['Telegram ID','Добавил (Telegram ID)','Добавлен (UTC)']},
  visit_reminders: {name:'Напоминания', fields:['visit_id','recipient_id','sent_at'], headers:['ID визита','Получатель (Telegram ID)','Отправлено (UTC)']},
  operations: {name:'Операции', fields:['operation_id','payload_hash','result_json','created_at'], headers:['ID операции','Хеш запроса','Результат (JSON)','Время (UTC)']}
};
var REGISTRY_WRITES = ['add_guest','update_guest','delete_guest','start_visit','stop_visit','remember_staff','bootstrap_employee_access','add_employee','mark_reminder_sent','import_state','create_backup','stage_photo','restore_backup'];

function registryError(code, message) { var e = new Error(message); e.registryCode = code; throw e; }
function registryAssert(ok, code, message) { if (!ok) registryError(code, message); }
function registryCopy(v) { return JSON.parse(JSON.stringify(v)); }
function registryInteger(v, minimum) { return typeof v === 'number' && Number.isSafeInteger(v) && v >= (minimum === undefined ? 1 : minimum); }
function registryId(v) { registryAssert(registryInteger(v), 'VALIDATION', 'Некорректный идентификатор.'); return v; }
function registryStrip(v) {return v.replace(REGISTRY_PY_STRIP,'');}
function registryCollapse(v) {return v.split(REGISTRY_PY_SPACE).filter(function(part){return part!=='';}).join(' ');}
function registryText(v, maximum, allowEmpty) {
  registryAssert(typeof v === 'string' && Array.from(v).length <= maximum && (allowEmpty || registryStrip(v).length > 0) && v.indexOf('\u0000') < 0, 'VALIDATION', 'Некорректный текст.');
  return registryStrip(v);
}
function registryMoment(value) {
  if (value === undefined || value === null) return new Date().toISOString();
  registryAssert(typeof value === 'string' && /(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value)), 'VALIDATION', 'Время должно содержать часовой пояс.');
  return value;
}
function registryInstant(value) {var base=Date.parse(value),fraction=value.match(/\.(\d+)(?:Z|[+-]\d\d:\d\d)$/),micro=fraction?Number((fraction[1]+'000000').slice(0,6)):0;return base*1000+micro%1000;}
function registryName(v) {return registryCollapse(Array.from(v.normalize('NFKC')).map(function(ch){return Object.prototype.hasOwnProperty.call(REGISTRY_CASEFOLD,ch)?REGISTRY_CASEFOLD[ch]:ch.toLowerCase();}).join('').replace(/ё/g,'е'));}
function registryDecimal(ch) {
  var code=ch.codePointAt(0),low=0,high=REGISTRY_DECIMAL_BASES.length-1;
  while(low<=high){var middle=(low+high)>>1,base=REGISTRY_DECIMAL_BASES[middle];if(code<base)high=middle-1;else if(code>base+9)low=middle+1;else return code-base;}
  return null;
}
// null means invalid formatting; empty digits remain valid for partial searches.
function registryPhoneDigits(value) {
  if(!value)return null;var chars=Array.from(value),digits='';
  for(var i=0;i<chars.length;i++){var ch=chars[i],digit=registryDecimal(ch);if(digit!==null)digits+=String(digit);else if('()+.-'.indexOf(ch)<0&&!REGISTRY_PY_SPACE.test(ch))return null;}
  return digits;
}
function registryPhone(v) {
  registryAssert(typeof v === 'string' && Array.from(v).length <= 100, 'VALIDATION', 'Укажите телефон цифрами.');
  var digits = registryPhoneDigits(v);registryAssert(digits!==null,'VALIDATION','Укажите телефон цифрами.');
  registryAssert(digits.length >= 7 && digits.length <= 15, 'VALIDATION', 'В телефоне должно быть от 7 до 15 цифр.');
  return digits.length === 11 && digits.charAt(0) === '8' ? '7' + digits.slice(1) : digits;
}
function registryPage(p) {
  var limit = p.limit === undefined ? 8 : p.limit, offset = p.offset === undefined ? 0 : p.offset;
  registryAssert(registryInteger(limit) && limit <= 100 && registryInteger(offset,0), 'VALIDATION', 'Некорректная страница.');
  return {limit:limit, offset:offset};
}
function registryHex(bytes) { return bytes.map(function(b){return ('0'+((b+256)%256).toString(16)).slice(-2);}).join(''); }
function registryHash(text) { return registryHex(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,text,Utilities.Charset.UTF_8)); }
function registryEqual(a,b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  var difference = 0; for(var i=0;i<a.length;i++) difference |= a.charCodeAt(i)^b.charCodeAt(i);
  return difference === 0;
}
function registryConfig() {
  var p = PropertiesService.getScriptProperties().getProperties();
  registryAssert(p.REGISTRY_OWNER_EMAIL === registryOwner() && String(p.REGISTRY_ADMIN_ID) === String(REGISTRY_ADMIN), 'CONFIG', 'Владелец хранилища не настроен.');
  registryAssert(Session.getEffectiveUser().getEmail().toLowerCase() === registryOwner(), 'CONFIG', 'Скрипт запущен не от владельца хранилища.');
  registryAssert(p.REGISTRY_SPREADSHEET_ID && p.REGISTRY_PHOTO_FOLDER_ID && p.REGISTRY_BACKUP_FOLDER_ID && p.REGISTRY_SECRET && p.REGISTRY_SECRET.length >= 32, 'CONFIG', 'Хранилище не настроено.');
  ['SPREADSHEET','PHOTO_FOLDER','BACKUP_FOLDER'].forEach(function(k){registryAssert(p['REGISTRY_'+k+'_ID']===p['REGISTRY_BOUND_'+k+'_ID'],'CONFIG','Запрещена автоматическая смена файлов хранилища.');});
  return p;
}
function registryPrivate(item) {
  try {
    var owner=registryOwner(),id=item.getId(),actual=item.getOwner();
    registryAssert(actual&&actual.getEmail().toLowerCase()===owner&&item.getSharingAccess()===DriveApp.Access.PRIVATE,'CONFIG','Проверьте владельца и закрытый доступ к файлам.');
    if(REGISTRY_PRIVATE_CACHE&&REGISTRY_PRIVATE_CACHE[id])return;
    // DriveApp PRIVATE excludes individually granted users; its editor/viewer
    // methods also omit Google Groups. Read the complete real ACL instead.
    var token='',seen=Object.create(null),owners=0,pages=0;
    do {
      registryAssert(++pages<=1000&&!seen[token],'CONFIG','Не удалось полностью проверить права файла.');seen[token]=true;
      var options={pageSize:100,supportsAllDrives:true,includePermissionsForView:'published',fields:'nextPageToken,permissions(id,type,role,emailAddress,deleted,pendingOwner)'};
      if(token)options.pageToken=token;
      var response=Drive.Permissions.list(id,options);
      registryAssert(response&&Array.isArray(response.permissions),'CONFIG','Не удалось полностью проверить права файла.');
      response.permissions.forEach(function(permission){
        registryAssert(permission&&permission.type==='user'&&permission.role==='owner'&&typeof permission.emailAddress==='string'&&permission.emailAddress.toLowerCase()===owner&&!permission.deleted&&!permission.pendingOwner,'CONFIG','К файлам картотеки должен иметь доступ только владелец.');owners++;
      });
      registryAssert(response.nextPageToken===undefined||typeof response.nextPageToken==='string','CONFIG','Не удалось полностью проверить права файла.');
      token=response.nextPageToken||'';
    } while(token);
    registryAssert(owners===1,'CONFIG','Не удалось подтвердить единственного владельца файла.');
    if(REGISTRY_PRIVATE_CACHE)REGISTRY_PRIVATE_CACHE[id]=true;
  } catch(error) {registryError('CONFIG','Не удалось подтвердить закрытый доступ только для владельца. Проверьте права и включение Drive API.');}
}

/** Run once, manually as the owner, after importing the blank workbook.
 * Set the three IDs and OWNER_EMAIL/ADMIN_ID in Script Properties beforehand.
 * A missing secret is generated privately in Script Properties, never logged.
 */
function setup() {
  registryAssert(Session.getEffectiveUser().getEmail().toLowerCase() === registryOwner(), 'PERMISSION', 'Настройка доступна только владельцу.');
  REGISTRY_PRIVATE_CACHE=Object.create(null);
  var lock = LockService.getScriptLock(); lock.waitLock(20000);
  try {
    var service = PropertiesService.getScriptProperties(), p = service.getProperties();
    ['SPREADSHEET','PHOTO_FOLDER','BACKUP_FOLDER'].forEach(function(k){var pinned=p['REGISTRY_BOUND_'+k+'_ID'];registryAssert(!pinned||pinned===p['REGISTRY_'+k+'_ID'],'CONFIG','Запрещена автоматическая смена файлов хранилища.');});
    registryAssert(p.REGISTRY_OWNER_EMAIL === registryOwner(), 'CONFIG', 'Нельзя изменить владельца существующего хранилища.');
    registryAssert(!p.REGISTRY_ADMIN_ID || String(p.REGISTRY_ADMIN_ID) === String(REGISTRY_ADMIN), 'CONFIG', 'Нельзя изменить администратора.');
    if (!p.REGISTRY_SPREADSHEET_ID) {
      var fresh = SpreadsheetApp.create('PC Arena — Картотека гостей');
      var initial = fresh.getSheets()[0]; initial.setName(REGISTRY_TABLES.guests.name);
      Object.keys(REGISTRY_TABLES).forEach(function(key){ var spec=REGISTRY_TABLES[key]; var sheet=fresh.getSheetByName(spec.name)||fresh.insertSheet(spec.name); sheet.getRange(1,1,1,spec.headers.length).setValues([spec.headers]); });
      p.REGISTRY_SPREADSHEET_ID=fresh.getId();
    }
    if (!p.REGISTRY_PHOTO_FOLDER_ID) p.REGISTRY_PHOTO_FOLDER_ID=DriveApp.createFolder('PC Arena — Фотографии гостей').getId();
    if (!p.REGISTRY_BACKUP_FOLDER_ID) p.REGISTRY_BACKUP_FOLDER_ID=DriveApp.createFolder('PC Arena — Резервные копии').getId();
    if (!p.REGISTRY_BACKUP_ARCHIVE_FOLDER_ID) p.REGISTRY_BACKUP_ARCHIVE_FOLDER_ID=DriveApp.getFolderById(p.REGISTRY_BACKUP_FOLDER_ID).createFolder('Архив фотографий — не удалять').getId();
    p.REGISTRY_OWNER_EMAIL=registryOwner(); p.REGISTRY_ADMIN_ID=String(REGISTRY_ADMIN);
    if (!p.REGISTRY_SECRET) p.REGISTRY_SECRET=Utilities.getUuid().replace(/-/g,'')+Utilities.getUuid().replace(/-/g,'');
    registryPrivate(DriveApp.getFileById(p.REGISTRY_SPREADSHEET_ID));
    registryPrivate(DriveApp.getFolderById(p.REGISTRY_PHOTO_FOLDER_ID));
    registryPrivate(DriveApp.getFolderById(p.REGISTRY_BACKUP_FOLDER_ID));
    registryPrivate(DriveApp.getFolderById(p.REGISTRY_BACKUP_ARCHIVE_FOLDER_ID));
    var state=registryRead(p,true);
    if (state.tables.meta.length) {
      registryAssert(registryMeta(state,'schema_version') === '1' && registryMeta(state,'owner_email') === registryOwner() && registryMeta(state,'admin_id') === String(REGISTRY_ADMIN), 'CONFIG', 'Существующая схема не совпадает.');
    } else {
      registryAssert(Object.keys(state.tables).every(function(k){return k==='meta'||state.tables[k].length===0;}), 'CONFIG', 'Инициализация разрешена только для пустой таблицы.');
      [['schema_version','1'],['owner_email',registryOwner()],['admin_id',String(REGISTRY_ADMIN)],['access_seeded','false'],['next_guest_id','1'],['next_visit_id','1'],['next_audit_id','1']].forEach(function(kv){state.tables.meta.push({key:kv[0],value:kv[1]});});
      state.dirty.meta=true; registryCommit(p,state);
    }
    ['SPREADSHEET','PHOTO_FOLDER','BACKUP_FOLDER'].forEach(function(k){p['REGISTRY_BOUND_'+k+'_ID']=p['REGISTRY_'+k+'_ID'];});
    service.setProperties(p,false);
    Object.keys(state.sheets).forEach(function(k){
      var sheet=state.sheets[k];sheet.setFrozenRows(1);
      var protections=sheet.getProtections(SpreadsheetApp.ProtectionType.SHEET),protection=protections.length?protections[0]:sheet.protect();
      protection.setDescription('PC Arena: изменения через бота').setWarningOnly(false);
      protection.removeEditors(protection.getEditors());if(protection.canDomainEdit())protection.setDomainEdit(false);
    });
    var guestsSheet=state.sheets.guests;guestsSheet.setFrozenColumns(2);if(!guestsSheet.getFilter())guestsSheet.getRange(1,1,guestsSheet.getMaxRows(),REGISTRY_TABLES.guests.fields.length).createFilter();
    guestsSheet.hideColumns(3);guestsSheet.hideColumns(5);
    ['meta','operations','visit_reminders'].forEach(function(k){state.sheets[k].hideSheet();});
    return {spreadsheet_id:p.REGISTRY_SPREADSHEET_ID,photo_folder_id:p.REGISTRY_PHOTO_FOLDER_ID,backup_folder_id:p.REGISTRY_BACKUP_FOLDER_ID,owner_email:registryOwner(),admin_id:REGISTRY_ADMIN,ready:true};
  } finally {REGISTRY_PRIVATE_CACHE=null;lock.releaseLock();}
}

function registryRead(config, allowEmptyMeta) {
  registryPrivate(DriveApp.getFileById(config.REGISTRY_SPREADSHEET_ID));
  ['PHOTO_FOLDER','BACKUP_FOLDER','BACKUP_ARCHIVE_FOLDER'].forEach(function(k){registryAssert(config['REGISTRY_'+k+'_ID'],'CONFIG','Закрытая папка хранилища не настроена.');registryPrivate(DriveApp.getFolderById(config['REGISTRY_'+k+'_ID']));});
  var book=SpreadsheetApp.openById(config.REGISTRY_SPREADSHEET_ID), state={tables:{},sheets:{},oldCounts:{},oldValues:{},dirty:{}};
  Object.keys(REGISTRY_TABLES).forEach(function(key){
    var spec=REGISTRY_TABLES[key], sheet=book.getSheetByName(spec.name);
    registryAssert(sheet, 'SCHEMA', 'Лист хранилища отсутствует. Создание пустой замены запрещено.');
    var values=sheet.getDataRange().getValues();
    registryAssert(values.length && spec.headers.every(function(v,i){return values[0][i]===v;}) && !values[0].slice(spec.headers.length).some(function(v){return v!=='';}), 'SCHEMA', 'Заголовки хранилища изменены.');
    state.tables[key]=[]; state.sheets[key]=sheet; state.oldCounts[key]=Math.max(0,values.length-1);
    state.oldValues[key]=values.slice(1).map(function(row){return spec.fields.map(function(f,i){return row[i]===null||row[i]===undefined?'':String(row[i]);});});
    values.slice(1).forEach(function(row){
      if(row.every(function(v){return v===''||v===null;})) return;
      var record={}; spec.fields.forEach(function(field,i){record[field]=row[i]===undefined?'':row[i];});
      spec.fields.forEach(function(field){
        if(['id','guest_id','actor_id','user_id','added_by','recipient_id','visit_id','created_by','updated_by','started_by','version'].indexOf(field)>=0) {
          if(record[field]==='' && field==='actor_id') record[field]=null;
          else {record[field]=Number(record[field]);registryId(record[field]);}
        } else if(field==='stopped_by') {record[field]=record[field]===''?null:Number(record[field]);if(record[field]!==null)registryId(record[field]);}
        else if(field==='stopped_at') record[field]=record[field]===''?null:String(record[field]);
        else if(field==='changes') {try{record[field]=JSON.parse(String(record[field]));}catch(e){registryError('SCHEMA','Повреждена запись истории.');}}
        else {registryAssert(typeof record[field]==='string', 'SCHEMA','Ячейки хранилища должны содержать текст.');}
      });
      state.tables[key].push(record);
    });
  });
  if(!allowEmptyMeta) {
    registryAssert(registryMeta(state,'schema_version')==='1' && registryMeta(state,'owner_email')===registryOwner() && registryMeta(state,'admin_id')===String(REGISTRY_ADMIN), 'SCHEMA','Метаданные хранилища отсутствуют или изменены.');
    ['next_guest_id','next_visit_id','next_audit_id'].forEach(function(k){registryId(Number(registryMeta(state,k)));});
    registryValidateState(state);
    registryAssert(registryMeta(state,'state_hash')===registryIntegrity(state),'SCHEMA','Данные таблицы изменены вне бота. Автоматическая замена запрещена.');
  }
  return state;
}
function registryValidateState(s) {
  Object.keys(REGISTRY_TABLES).forEach(function(key){var spec=REGISTRY_TABLES[key];s.tables[key].forEach(function(row){
    registryAssert(Object.keys(row).length===spec.fields.length&&spec.fields.every(function(k){return Object.prototype.hasOwnProperty.call(row,k);}), 'SCHEMA','Повреждена структура записи.');
    spec.fields.forEach(function(field){var value=row[field];if(['id','guest_id','actor_id','user_id','added_by','recipient_id','visit_id','created_by','updated_by','started_by','version'].indexOf(field)>=0){if(!(field==='actor_id'&&value===null))registryId(value);}else if(field==='stopped_by'){if(value!==null)registryId(value);}else if(field==='changes'){registryAssert(value&&typeof value==='object'&&!Array.isArray(value),'SCHEMA','Повреждены изменения.');}else if(field==='stopped_at'){registryAssert(value===null||typeof value==='string','SCHEMA','Повреждена дата.');}else registryAssert(typeof value==='string','SCHEMA','Некорректный тип ячейки.');});
  });});
  var guestIds={},phoneKeys={},visitIds={},active={};
  s.tables.guests.forEach(function(g){
    registryAssert(!guestIds[g.id]&&!phoneKeys[g.phone_key], 'SCHEMA','Повторяются карточки или телефоны.');guestIds[g.id]=true;phoneKeys[g.phone_key]=true;
    registryAssert(g.name && Array.from(g.name).length<=100 && Array.from(g.comment).length<=3000 && Array.from(g.entry_reason).length<=3000 && ['open','closed'].indexOf(g.entry_status)>=0 && registryPhone(g.phone)===g.phone_key && g.photo_drive_id, 'SCHEMA','Повреждена карточка.');
    registryMoment(g.created_at);registryMoment(g.updated_at);
  });
  s.tables.visits.forEach(function(v){registryAssert(guestIds[v.guest_id]&&!visitIds[v.id], 'SCHEMA','Повреждена связь посещения.');visitIds[v.id]=true;registryMoment(v.started_at);if(v.stopped_at){registryMoment(v.stopped_at);registryAssert(v.stopped_by!==null&&registryInstant(v.stopped_at)>=registryInstant(v.started_at),'SCHEMA','Повреждено посещение.');}else{registryAssert(v.stopped_by===null&&!active[v.guest_id],'SCHEMA','Несколько активных визитов.');active[v.guest_id]=true;}});
  s.tables.visit_reminders.forEach(function(r){registryAssert(visitIds[r.visit_id], 'SCHEMA','Повреждена отметка напоминания.');registryMoment(r.sent_at);});
  var accessIds={};s.tables.employee_access.forEach(function(e){registryAssert(e.user_id!==REGISTRY_ADMIN&&!accessIds[e.user_id]&&e.added_by===REGISTRY_ADMIN,'SCHEMA','Повреждены права доступа.');accessIds[e.user_id]=true;registryMoment(e.added_at);});
  var auditIds={};s.tables.audit_events.forEach(function(e){registryId(e.id);registryId(e.guest_id);if(e.actor_id!==null)registryId(e.actor_id);registryAssert(!auditIds[e.id]&&['add','update','delete','start','stop'].indexOf(e.action)>=0&&typeof e.guest_name==='string'&&e.changes&&typeof e.changes==='object'&&!Array.isArray(e.changes),'SCHEMA','Повреждена история изменений.');auditIds[e.id]=true;registryMoment(e.created_at);});
  var lastAudit={};s.tables.audit_events.forEach(function(e){if(!lastAudit[e.guest_id]||lastAudit[e.guest_id].id<e.id)lastAudit[e.guest_id]=e;});Object.keys(lastAudit).forEach(function(id){registryAssert((lastAudit[id].action==='delete')===!guestIds[id],'SCHEMA','Карточки и история не совпадают.');});
  var staffIds={};s.tables.staff.forEach(function(e){registryId(e.actor_id);registryAssert(!staffIds[e.actor_id]&&typeof e.name==='string'&&registryStrip(e.name)&&typeof e.username==='string','SCHEMA','Повреждены профили сотрудников.');staffIds[e.actor_id]=true;registryMoment(e.updated_at);});
  var operationIds={};s.tables.operations.forEach(function(e){registryAssert(typeof e.operation_id==='string'&&/^[a-f0-9]{32}$/.test(e.operation_id)&&!operationIds[e.operation_id]&&/^[a-f0-9]{64}$/.test(e.payload_hash),'SCHEMA','Поврежден журнал операций.');operationIds[e.operation_id]=true;try{JSON.parse(e.result_json);}catch(error){registryError('SCHEMA','Поврежден результат операции.');}registryMoment(e.created_at);});
  [['guests','next_guest_id'],['visits','next_visit_id'],['audit_events','next_audit_id']].forEach(function(pair){var next=Number(registryMeta(s,pair[1]));registryAssert(s.tables[pair[0]].every(function(r){return r.id<next;}),'SCHEMA','Счётчик хранилища изменён.');});
}
function registryMeta(s,key) {var rows=s.tables.meta.filter(function(r){return r.key===key;});registryAssert(rows.length<=1,'SCHEMA','Повторяются настройки.');return rows.length?String(rows[0].value):null;}
function registryIntegrity(s) {var canonical={};Object.keys(REGISTRY_TABLES).sort().forEach(function(key){var spec=REGISTRY_TABLES[key];canonical[key]=s.tables[key].filter(function(r){return key!=='meta'||r.key!=='state_hash';}).map(function(row){return JSON.stringify(spec.fields.map(function(f){return row[f];}));}).sort();});return registryHash(JSON.stringify(canonical));}
function registrySetMeta(s,key,value) {var row=s.tables.meta.find(function(r){return r.key===key;});if(row)row.value=String(value);else s.tables.meta.push({key:key,value:String(value)});s.dirty.meta=true;}
function registryNext(s,key) {var value=registryId(Number(registryMeta(s,key)));registrySetMeta(s,key,value+1);return value;}
function registryCell(value) { if(value===null||value===undefined)return {}; var text=typeof value==='object'?JSON.stringify(value):String(value);registryAssert(text.length<=49000,'LIMIT','Запись превышает размер ячейки.');return {userEnteredValue:{stringValue:text}}; }
function registryCommit(config,state) {
  registrySetMeta(state,'state_hash',registryIntegrity(state));
  var requests=[];
  Object.keys(state.dirty).forEach(function(key){if(!state.dirty[key])return;var spec=REGISTRY_TABLES[key],records=state.tables[key];var needed=records.length+1, sheet=state.sheets[key];if(sheet.getMaxRows()<needed)requests.push({appendDimension:{sheetId:sheet.getSheetId(),dimension:'ROWS',length:needed-sheet.getMaxRows()}});
    var rows=records.map(function(r){return {values:spec.fields.map(function(f){return registryCell(r[f]);})};}),end=Math.max(state.oldCounts[key],records.length),group=-1;
    function pushRange(until){if(group<0)return;var output=[];for(var index=group;index<until;index++)output.push(rows[index]||{values:spec.fields.map(function(){return {};})});requests.push({updateCells:{range:{sheetId:sheet.getSheetId(),startRowIndex:group+1,endRowIndex:until+1,startColumnIndex:0,endColumnIndex:spec.fields.length},rows:output,fields:'userEnteredValue'}});group=-1;}
    for(var index=0;index<end;index++){var old=state.oldValues[key][index]||[],current=rows[index]?rows[index].values.map(function(c){return c.userEnteredValue?c.userEnteredValue.stringValue:'';}):[];var changed=spec.fields.some(function(f,column){return (old[column]||'')!==(current[column]||'');});if(changed&&group<0)group=index;if(!changed)pushRange(index);}pushRange(end);
  });
  if(requests.length)Sheets.Spreadsheets.batchUpdate({requests:requests},config.REGISTRY_SPREADSHEET_ID);
}
function registryActor(s,id) {registryId(id);registryAssert(id===REGISTRY_ADMIN||s.tables.employee_access.some(function(r){return r.user_id===id;}),'PERMISSION','Нет доступа сотрудника.');}
function registryGuest(s,id,version) {registryId(id);var g=s.tables.guests.find(function(r){return r.id===id;});registryAssert(g&&(version===undefined||g.version===version),'STALE_GUEST','Карточка уже изменена или удалена. Откройте её заново.');return g;}
function registryPhoto(config, encoded) {
  registryAssert(typeof encoded==='string'&&encoded.length>0&&encoded.length%4===0&&encoded.length<=Math.ceil(REGISTRY_MAX_PHOTO/3)*4&&!/[^A-Za-z0-9+/=]/.test(encoded),'VALIDATION','Некорректное фото.');
  var padding=encoded.endsWith('==')?2:encoded.endsWith('=')?1:0;
  registryAssert(encoded.slice(0,encoded.length-padding).indexOf('=')<0,'VALIDATION','Некорректное фото.');
  var bytes=Utilities.base64Decode(encoded);registryAssert(bytes.length>0&&bytes.length<=REGISTRY_MAX_PHOTO,'VALIDATION','Нужно фото размером не более 10 МБ.');
  var file=DriveApp.getFolderById(config.REGISTRY_PHOTO_FOLDER_ID).createFile(Utilities.newBlob(bytes,'application/octet-stream','guest-'+Utilities.getUuid()+'.jpg'));
  file.setDescription('pc-arena-photo:v1:'+config.REGISTRY_SPREADSHEET_ID+':'+registryHex(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,bytes)));registryPrivate(file);return file.getId();
}
function registryPhotoBytes(config,ref) {
  registryAssert(typeof ref==='string'&&/^[A-Za-z0-9_-]{8,200}$/.test(ref),'VALIDATION','Некорректная ссылка на фото.');
  var file=DriveApp.getFileById(ref),parents=file.getParents(),found=false;while(parents.hasNext())if(parents.next().getId()===config.REGISTRY_PHOTO_FOLDER_ID)found=true;
  var prefix='pc-arena-photo:v1:'+config.REGISTRY_SPREADSHEET_ID+':',description=file.getDescription();registryAssert(found&&description.indexOf(prefix)===0&&/^[a-f0-9]{64}$/.test(description.slice(prefix.length)),'PERMISSION','Фото не относится к картотеке.');registryPrivate(file);
  var bytes=file.getBlob().getBytes();registryAssert(bytes.length<=REGISTRY_MAX_PHOTO,'LIMIT','Фото превышает допустимый размер.');registryAssert(registryHex(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,bytes))===description.slice(prefix.length),'SCHEMA','Фотография изменена или повреждена.');return Utilities.base64Encode(bytes);
}
function registryWireGuest(config,g,includePhoto) {
  if(!g)return null;var result=registryCopy(g);delete result.name_key;delete result.photo_drive_id;result.photo=includePhoto?registryPhotoBytes(config,g.photo_drive_id):'';return result;
}
function registryGuestResult(g) {return {__registry_guest_result:true,guest:registryCopy(g)};}
function registryMaterialize(config,result) {return result&&result.__registry_guest_result?registryWireGuest(config,result.guest,true):result;}
function registryAudit(s,g,actor,action,changes,at) {s.tables.audit_events.push({id:registryNext(s,'next_audit_id'),guest_id:g.id,guest_name:g.name,actor_id:actor===undefined?null:actor,action:action,changes:changes,created_at:at});s.dirty.audit_events=true;}
function registryChanges(old,g) {var changes={};['name','phone','comment','entry_status','entry_reason'].forEach(function(k){var a=old?old[k]:null,b=g?g[k]:null;if(a!==b)changes[k]={before:a,after:b};});if(!old||!g||old.photo_drive_id!==g.photo_drive_id||old.photo_file_id!==g.photo_file_id)changes.photo={before:old?'Есть фото':null,after:g?(old?'Новое фото':'Есть фото'):null};return changes;}
function registryValidateGuest(config,p,current) {
  var g=current?registryCopy(current):{};
  var name=p.name===undefined?g.name:p.name;registryAssert(typeof name==='string','VALIDATION','Имя должно быть текстом.');g.name=registryText(registryCollapse(name),100,false);g.name_key=registryName(g.name);
  g.phone=registryText(p.phone===undefined?g.phone:p.phone,100,false);g.phone_key=registryPhone(g.phone);
  g.comment=registryText(p.comment===undefined?g.comment:p.comment,3000,true);
  g.photo_file_id=registryText(p.photo_file_id===undefined?g.photo_file_id:p.photo_file_id,1024,false);
  g.entry_status=p.entry_status===undefined?(g.entry_status||'open'):p.entry_status;registryAssert(['open','closed'].indexOf(g.entry_status)>=0,'VALIDATION','Некорректный статус входа.');
  g.entry_reason=registryText(p.entry_reason===undefined?(g.entry_reason||''):p.entry_reason,3000,true);
  if(p.photo!==undefined)g.photo_drive_id=registryPhoto(config,p.photo);
  registryAssert(g.photo_drive_id,'VALIDATION','Нужно фото гостя.');return g;
}
function registrySeconds(visit,now) {return Math.max(0,Math.floor((registryInstant(visit.stopped_at||now)-registryInstant(visit.started_at))/1000000));}
function registrySearch(s,query) {registryAssert(typeof query==='string'&&Array.from(query).length<=100,'VALIDATION','Поисковый запрос слишком длинный.');var name=registryName(query);if(!name)return [];var digits=registryPhoneDigits(query)||'';if(digits.length===11&&digits[0]==='8')digits='7'+digits.slice(1);return s.tables.guests.filter(function(g){return g.name_key.indexOf(name)>=0||(digits&&g.phone_key.indexOf(digits)>=0);}).sort(function(a,b){return a.name_key<b.name_key?-1:a.name_key>b.name_key?1:a.id-b.id;});}

function registryDispatch(config,s,action,p) {
  var at,g,old,v,page,rows;
  switch(action) {
    case 'health': registryPrivate(DriveApp.getFileById(config.REGISTRY_SPREADSHEET_ID));return {version:1,schema_version:1,ready:true,owner_email:registryOwner(),admin_id:REGISTRY_ADMIN};
    case 'get_photo': return registryPhotoBytes(config,p.photo_ref);
    case 'stage_photo': registryAssert(p.admin_id===REGISTRY_ADMIN,'PERMISSION','Подготовка фотографий доступна только администратору.');var staged=registryPhoto(config,p.photo);return {photo_ref:staged,bytes:Utilities.base64Decode(p.photo).length};
    case 'get_guest': g=s.tables.guests.find(function(r){return r.id===registryId(p.id);});return registryWireGuest(config,g,true);
    case 'find_phone': var phone=registryPhone(p.phone);g=s.tables.guests.find(function(r){return r.phone_key===phone;});return registryWireGuest(config,g,true);
    case 'search_guests': page=registryPage(p);return registrySearch(s,p.query).slice(page.offset,page.offset+page.limit).map(function(r){return registryWireGuest(config,r,false);});
    case 'count_search': return registrySearch(s,p.query).length;
    case 'add_guest':
      registryActor(s,p.actor_id);at=registryMoment();
      registryAssert(!s.tables.guests.some(function(r){return r.phone_key===registryPhone(p.phone);}), 'DUPLICATE_PHONE','Гость с таким телефоном уже есть.');
      g=registryValidateGuest(config,p,null);g.id=registryNext(s,'next_guest_id');g.created_by=p.actor_id;g.updated_by=p.actor_id;g.created_at=at;g.updated_at=at;g.version=1;s.tables.guests.push(g);s.dirty.guests=true;registryAudit(s,g,p.actor_id,'add',registryChanges(null,g),at);return registryGuestResult(g);
    case 'update_guest':
      registryActor(s,p.actor_id);old=registryGuest(s,p.id,registryId(p.expected_version));
      registryAssert(p.fields&&typeof p.fields==='object'&&!Array.isArray(p.fields)&&Object.keys(p.fields).length>0&&Object.keys(p.fields).every(function(k){return ['name','phone','comment','photo','photo_file_id','entry_status','entry_reason'].indexOf(k)>=0;}),'VALIDATION','Некорректные поля карточки.');
      if(p.fields.phone!==undefined)registryAssert(!s.tables.guests.some(function(r){return r.id!==old.id&&r.phone_key===registryPhone(p.fields.phone);}), 'DUPLICATE_PHONE','Гость с таким телефоном уже есть.');
      g=registryValidateGuest(config,p.fields,old);g.version=old.version+1;g.updated_by=p.actor_id;g.updated_at=registryMoment();s.tables.guests[s.tables.guests.indexOf(old)]=g;s.dirty.guests=true;registryAudit(s,g,p.actor_id,'update',registryChanges(old,g),g.updated_at);return registryGuestResult(g);
    case 'delete_guest':
      registryActor(s,p.actor_id);g=registryGuest(s,p.id,registryId(p.expected_version));var removed={};s.tables.visits.forEach(function(r){if(r.guest_id===g.id)removed[r.id]=true;});s.tables.guests=s.tables.guests.filter(function(r){return r.id!==g.id;});s.tables.visits=s.tables.visits.filter(function(r){return r.guest_id!==g.id;});s.tables.visit_reminders=s.tables.visit_reminders.filter(function(r){return !removed[r.visit_id];});s.dirty.guests=s.dirty.visits=s.dirty.visit_reminders=true;registryAudit(s,g,p.actor_id,'delete',registryChanges(g,null),registryMoment());return true;
    case 'start_visit':
      registryActor(s,p.actor_id);g=registryGuest(s,p.guest_id,registryId(p.expected_version));registryAssert(!s.tables.visits.some(function(r){return r.guest_id===g.id&&r.stopped_at===null;}),'VISIT_STATE','Отсчёт времени уже запущен.');at=registryMoment(p.now);v={id:registryNext(s,'next_visit_id'),guest_id:g.id,started_at:at,stopped_at:null,started_by:p.actor_id,stopped_by:null};s.tables.visits.push(v);g.version++;g.updated_at=at;g.updated_by=p.actor_id;s.dirty.guests=s.dirty.visits=true;registryAudit(s,g,p.actor_id,'start',{visit_id:{before:null,after:v.id},started_at:{before:null,after:at}},at);return v;
    case 'stop_visit':
      registryActor(s,p.actor_id);registryId(p.guest_id);registryId(p.visit_id);v=s.tables.visits.find(function(r){return r.id===p.visit_id&&r.guest_id===p.guest_id&&r.stopped_at===null;});registryAssert(v,'VISIT_STATE','Этот отсчёт уже завершён или удалён.');at=registryMoment(p.now);registryAssert(registryInstant(at)>=registryInstant(v.started_at),'VALIDATION','Время завершения раньше начала.');v.stopped_at=at;v.stopped_by=p.actor_id;g=registryGuest(s,p.guest_id);g.version++;g.updated_at=at;g.updated_by=p.actor_id;s.dirty.guests=s.dirty.visits=true;registryAudit(s,g,p.actor_id,'stop',{visit_id:{before:v.id,after:v.id},stopped_at:{before:null,after:at}},at);return v;
    case 'get_visit_summary':
      registryId(p.guest_id);at=registryMoment(p.now);rows=s.tables.visits.filter(function(r){return r.guest_id===p.guest_id;});v=rows.find(function(r){return r.stopped_at===null;})||null;var completed=rows.filter(function(r){return r.stopped_at!==null;});return {active:v,completed_count:completed.length,completed_seconds:completed.reduce(function(n,r){return n+registrySeconds(r,at);},0),current_seconds:v?registrySeconds(v,at):0};
    case 'list_visits':
      registryId(p.guest_id);page=registryPage({limit:p.limit===undefined?5:p.limit,offset:p.offset});return s.tables.visits.filter(function(r){return r.guest_id===p.guest_id;}).sort(function(a,b){return Date.parse(b.started_at)-Date.parse(a.started_at)||b.id-a.id;}).slice(page.offset,page.offset+page.limit);
    case 'list_audit':
      if(p.guest_id!==undefined&&p.guest_id!==null)registryId(p.guest_id);page=registryPage({limit:p.limit===undefined?5:p.limit,offset:p.offset});return s.tables.audit_events.filter(function(r){return p.guest_id===undefined||p.guest_id===null||r.guest_id===p.guest_id;}).sort(function(a,b){return b.id-a.id;}).slice(page.offset,page.offset+page.limit);
    case 'count_audit': if(p.guest_id!==undefined&&p.guest_id!==null)registryId(p.guest_id);return s.tables.audit_events.filter(function(r){return p.guest_id===undefined||p.guest_id===null||r.guest_id===p.guest_id;}).length;
    case 'get_audit': registryId(p.event_id);return s.tables.audit_events.find(function(r){return r.id===p.event_id;})||null;
    case 'remember_staff':
      registryActor(s,p.actor_id);var name=registryCollapse(registryText(p.name,200,false)),username=p.username===null||p.username===undefined?'':registryText(p.username,65,true).replace(/^@/,'');registryAssert(!username||/^[A-Za-z0-9_]{1,64}$/.test(username),'VALIDATION','Некорректный Telegram username.');old=s.tables.staff.find(function(r){return r.actor_id===p.actor_id;});if(old&&old.name===name&&old.username===username)return null;g={actor_id:p.actor_id,name:name,username:username,updated_at:registryMoment()};if(old)s.tables.staff[s.tables.staff.indexOf(old)]=g;else s.tables.staff.push(g);s.dirty.staff=true;return null;
    case 'staff_label':
      if(p.actor_id===null||p.actor_id===undefined)return 'Сотрудник не указан';registryId(p.actor_id);g=s.tables.staff.find(function(r){return r.actor_id===p.actor_id;});return g?(g.name+(g.username?' (@'+g.username+')':'')):'Telegram ID '+p.actor_id;
    case 'bootstrap_employee_access':
      registryAssert(p.admin_id===REGISTRY_ADMIN,'PERMISSION','Администратор базы уже установлен.');registryAssert(Array.isArray(p.initial_ids),'VALIDATION','Некорректный список сотрудников.');p.initial_ids.forEach(function(id){if(id!==Math.pow(2,53))registryId(id);});if(registryMeta(s,'access_seeded')==='true')return null;
      at=registryMoment();p.initial_ids.forEach(function(id){if(id!==REGISTRY_ADMIN&&id!==Math.pow(2,53)&&!s.tables.employee_access.some(function(r){return r.user_id===id;}))s.tables.employee_access.push({user_id:id,added_by:REGISTRY_ADMIN,added_at:at});});s.dirty.employee_access=true;registrySetMeta(s,'access_seeded','true');return null;
    case 'add_employee':
      registryAssert(p.actor_id===REGISTRY_ADMIN&&registryMeta(s,'access_seeded')==='true','PERMISSION','Добавлять сотрудников может только администратор.');registryId(p.user_id);if(p.user_id===REGISTRY_ADMIN||s.tables.employee_access.some(function(r){return r.user_id===p.user_id;}))return false;s.tables.employee_access.push({user_id:p.user_id,added_by:REGISTRY_ADMIN,added_at:registryMoment()});s.dirty.employee_access=true;return true;
    case 'has_employee_access': registryId(p.user_id);return s.tables.employee_access.some(function(r){return r.user_id===p.user_id;});
    case 'employee_ids': return s.tables.employee_access.map(function(r){return r.user_id;}).sort(function(a,b){return a-b;});
    case 'list_employees': page=registryPage(p);return s.tables.employee_access.slice().sort(function(a,b){return Date.parse(a.added_at)-Date.parse(b.added_at)||a.user_id-b.user_id;}).slice(page.offset,page.offset+page.limit);
    case 'count_employees': return s.tables.employee_access.length;
    case 'overdue_visits':
      at=registryMoment(p.now);var threshold=p.threshold_seconds===undefined?86400:p.threshold_seconds;registryAssert(registryInteger(threshold),'VALIDATION','Некорректный порог напоминания.');return s.tables.visits.filter(function(r){return r.stopped_at===null&&registrySeconds(r,at)>=threshold;}).sort(function(a,b){return Date.parse(a.started_at)-Date.parse(b.started_at)||a.id-b.id;}).map(function(r){return {guest:registryWireGuest(config,registryGuest(s,r.guest_id),false),visit:r};});
    case 'reminder_sent': registryId(p.visit_id);registryId(p.recipient_id);return s.tables.visit_reminders.some(function(r){return r.visit_id===p.visit_id&&r.recipient_id===p.recipient_id;});
    case 'mark_reminder_sent':
      registryId(p.visit_id);registryActor(s,p.recipient_id);if(!s.tables.visits.some(function(r){return r.id===p.visit_id&&r.stopped_at===null;})||s.tables.visit_reminders.some(function(r){return r.visit_id===p.visit_id&&r.recipient_id===p.recipient_id;}))return false;s.tables.visit_reminders.push({visit_id:p.visit_id,recipient_id:p.recipient_id,sent_at:registryMoment(p.now)});s.dirty.visit_reminders=true;return true;
    case 'snapshot':
      at=registryMoment(p.now);return {generated_at:at,guests:s.tables.guests.map(function(r){var result=registryWireGuest(config,r,false);result.photo_ref=r.photo_drive_id;return result;}),visits:registryCopy(s.tables.visits)};
    case 'import_state': return registryImport(config,s,p);
    case 'create_backup': return registryBackup(config,s,p);
    case 'restore_backup': return registryRestore(config,s,p);
    default: registryError('VALIDATION','Неизвестное действие хранилища.');
  }
}

function registryImport(config,s,p) {
  registryAssert(p.admin_id===REGISTRY_ADMIN,'PERMISSION','Импорт доступен только администратору.');var input=p.state;
  registryAssert(input&&typeof input==='object'&&Object.keys(input).every(function(k){return ['guests','visits','audit_events','staff','employee_access','visit_reminders'].indexOf(k)>=0;}),'VALIDATION','Некорректный импорт.');
  registryAssert(registryMeta(s,'imported_at')===null&&['guests','visits','audit_events','staff','employee_access','visit_reminders'].every(function(k){return s.tables[k].length===0;}),'CONFLICT','Импорт допускается только в новое пустое хранилище.');
  ['guests','visits','audit_events','staff','employee_access','visit_reminders'].forEach(function(key){registryAssert(Array.isArray(input[key]),'VALIDATION','Отсутствует раздел импорта.');});
  s.tables.guests=input.guests.map(function(row){var fields=registryCopy(row),base=null;if(fields.photo_ref){registryAssert(fields.photo===undefined||fields.photo==='','VALIDATION','Укажите фото или подготовленную ссылку.');registryPhotoBytes(config,fields.photo_ref);base={photo_drive_id:fields.photo_ref};delete fields.photo;}var g=registryValidateGuest(config,fields,base);['id','created_by','updated_by','version'].forEach(function(k){g[k]=registryId(row[k]);});g.created_at=registryMoment(row.created_at);g.updated_at=registryMoment(row.updated_at);return g;});
  ['visits','audit_events','staff','employee_access','visit_reminders'].forEach(function(key){s.tables[key]=registryCopy(input[key]);s.dirty[key]=true;});s.dirty.guests=true;
  function maximum(rows,key){return rows.reduce(function(n,r){return Math.max(n,registryId(r[key]));},0);}
  var sequences=p.sequences||{};registryAssert(typeof sequences==='object'&&!Array.isArray(sequences)&&Object.keys(sequences).every(function(k){return ['guests','visits','audit_events'].indexOf(k)>=0&&registryInteger(sequences[k],0);}), 'VALIDATION','Некорректные счётчики импорта.');
  registrySetMeta(s,'next_guest_id',1+Math.max(maximum(s.tables.guests,'id'),maximum(s.tables.audit_events,'guest_id'),sequences.guests||0));
  registrySetMeta(s,'next_visit_id',1+Math.max(maximum(s.tables.visits,'id'),sequences.visits||0));
  registrySetMeta(s,'next_audit_id',1+Math.max(maximum(s.tables.audit_events,'id'),sequences.audit_events||0));
  registrySetMeta(s,'access_seeded','true');registrySetMeta(s,'imported_at',registryMoment());registryValidateState(s);
  return {imported:true,guests:s.tables.guests.length,visits:s.tables.visits.length,employees:s.tables.employee_access.length,audit_events:s.tables.audit_events.length};
}

/** A dated workbook plus a manifest of independently archived photo bytes.
 * Content-addressed photo copies are reused between snapshots to save free Drive
 * quota. Keep the photo archive alongside the dated snapshots. Only the final
 * manifest marks completion; incomplete folders are never advertised as usable.
 */
function registryBackup(config,s,p) {
  var started=Date.now(),stamp=registryMoment(),previous=registryMeta(s,'last_backup_receipt');
  registryAssert(p.only_if_due===undefined||typeof p.only_if_due==='boolean','VALIDATION','Некорректный режим резервного копирования.');
  if(p.only_if_due!==false&&previous) {var receipt=JSON.parse(previous);if(receipt.complete===true&&receipt.created_at.slice(0,10)===stamp.slice(0,10)){registryPrivate(DriveApp.getFileById(receipt.manifest_file_id));return receipt;}}
  var folder=DriveApp.getFolderById(config.REGISTRY_BACKUP_FOLDER_ID).createFolder('registry-'+stamp.replace(/[:.]/g,'-')+'-'+Utilities.getUuid());registryPrivate(folder);
  var copy=DriveApp.getFileById(config.REGISTRY_SPREADSHEET_ID).makeCopy('Карточки гостей — '+stamp,folder);registryPrivate(copy);
  registryAssert(config.REGISTRY_BACKUP_ARCHIVE_FOLDER_ID,'CONFIG','Архив фотографий не настроен.');var photoFolder=DriveApp.getFolderById(config.REGISTRY_BACKUP_ARCHIVE_FOLDER_ID);registryPrivate(photoFolder);
  var parents=photoFolder.getParents(),validParent=false;while(parents.hasNext())if(parents.next().getId()===config.REGISTRY_BACKUP_FOLDER_ID)validParent=true;registryAssert(validParent,'CONFIG','Архив фотографий находится вне резервного хранилища.');
  var manifest={version:1,created_at:stamp,owner_email:registryOwner(),admin_id:REGISTRY_ADMIN,source_spreadsheet_id:config.REGISTRY_SPREADSHEET_ID,spreadsheet_file_id:copy.getId(),photo_archive_folder_id:photoFolder.getId(),photos:[],counts:{guests:s.tables.guests.length,visits:s.tables.visits.length,audit_events:s.tables.audit_events.length,employees:s.tables.employee_access.length}},files=DriveApp.getFolderById(config.REGISTRY_PHOTO_FOLDER_ID).getFiles();
  while(files.hasNext()) {
    registryAssert(Date.now()-started<240000,'LIMIT','Копия не завершена: превышено время обработки фотографий.');var file=files.next();if(file.getDescription().indexOf('pc-arena-photo:v1:'+config.REGISTRY_SPREADSHEET_ID+':')!==0)continue;registryPrivate(file);
    var bytes=file.getBlob().getBytes(),digest=registryHex(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,bytes)),name='sha256-'+digest,candidates=photoFolder.getFilesByName(name),saved;
    registryAssert(file.getDescription()==='pc-arena-photo:v1:'+config.REGISTRY_SPREADSHEET_ID+':'+digest,'SCHEMA','Исходная фотография повреждена.');
    if(candidates.hasNext()){saved=candidates.next();registryAssert(!candidates.hasNext(),'SCHEMA','Архив фотографий содержит неоднозначные копии.');}
    else{saved=file.makeCopy(name,photoFolder);saved.setDescription('pc-arena-backup-photo:v1:'+config.REGISTRY_SPREADSHEET_ID+':'+digest);}
    registryPrivate(saved);registryAssert(saved.getDescription()==='pc-arena-backup-photo:v1:'+config.REGISTRY_SPREADSHEET_ID+':'+digest&&registryHex(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,saved.getBlob().getBytes()))===digest,'SCHEMA','Копия фотографии повреждена.');
    manifest.photos.push({original_file_id:file.getId(),backup_file_id:saved.getId(),sha256:digest,bytes:bytes.length});
  }
  var covered={};manifest.photos.forEach(function(photo){covered[photo.original_file_id]=true;});
  s.tables.guests.forEach(function(guest){registryAssert(covered[guest.photo_drive_id],'SCHEMA','Копия неполная: отсутствует фотография гостя.');});
  s.tables.operations.forEach(function(op){var result=JSON.parse(op.result_json),ref=result&&result.__registry_guest_result?result.guest.photo_drive_id:result&&result.photo_ref;registryAssert(!ref||covered[ref],'SCHEMA','Копия неполная: отсутствует сохранённая фотография.');});
  var manifestFile=folder.createFile(Utilities.newBlob(JSON.stringify(manifest,null,2),'application/json','manifest.complete.json'));registryPrivate(manifestFile);
  var result={complete:true,folder_id:folder.getId(),manifest_file_id:manifestFile.getId(),created_at:stamp,photo_count:manifest.photos.length,counts:manifest.counts};registrySetMeta(s,'last_backup_receipt',JSON.stringify(result));return result;
}

function registryHasParent(item,id) {var parents=item.getParents();while(parents.hasNext())if(parents.next().getId()===id)return true;return false;}
/** Explicit recovery into a fresh destination only. Never overwrite a live registry. */
function registryRestore(config,s,p) {
  registryAssert(p.admin_id===REGISTRY_ADMIN,'PERMISSION','Восстановление доступно только администратору.');
  registryAssert(typeof p.manifest_file_id==='string'&&/^[A-Za-z0-9_-]{8,200}$/.test(p.manifest_file_id),'VALIDATION','Некорректный манифест резервной копии.');
  registryAssert(registryMeta(s,'imported_at')===null&&['guests','visits','audit_events','staff','employee_access','visit_reminders'].every(function(k){return s.tables[k].length===0;}),'CONFLICT','Восстановление допускается только в новое пустое хранилище.');
  var started=Date.now(),file=DriveApp.getFileById(p.manifest_file_id);registryPrivate(file);registryAssert(file.getName()==='manifest.complete.json','VALIDATION','Копия не помечена завершённой.');
  var parents=file.getParents();registryAssert(parents.hasNext(),'PERMISSION','Манифест находится вне резервных копий.');var folder=parents.next();registryPrivate(folder);registryAssert(registryHasParent(folder,config.REGISTRY_BACKUP_FOLDER_ID),'PERMISSION','Манифест находится вне резервных копий.');
  var manifest=JSON.parse(file.getBlob().getDataAsString());registryAssert(manifest.version===1&&manifest.owner_email===registryOwner()&&manifest.admin_id===REGISTRY_ADMIN&&Array.isArray(manifest.photos),'VALIDATION','Некорректная резервная копия.');
  var workbook=DriveApp.getFileById(manifest.spreadsheet_file_id);registryPrivate(workbook);registryAssert(registryHasParent(workbook,folder.getId()),'PERMISSION','Таблица находится вне резервной копии.');
  var archive=DriveApp.getFolderById(manifest.photo_archive_folder_id);registryPrivate(archive);registryAssert(registryHasParent(archive,config.REGISTRY_BACKUP_FOLDER_ID),'PERMISSION','Архив фотографий находится вне резервных копий.');
  var sourceConfig=registryCopy(config);sourceConfig.REGISTRY_SPREADSHEET_ID=manifest.spreadsheet_file_id;var source=registryRead(sourceConfig,false),mapping={},copied={};
  registryAssert(manifest.counts.guests===source.tables.guests.length&&manifest.counts.visits===source.tables.visits.length&&manifest.counts.employees===source.tables.employee_access.length&&manifest.counts.audit_events===source.tables.audit_events.length,'SCHEMA','Состав резервной копии не совпадает с манифестом.');
  manifest.photos.forEach(function(photo){
    registryAssert(Date.now()-started<240000,'LIMIT','Восстановление не завершено: превышено время обработки фотографий.');
    registryAssert(typeof photo.original_file_id==='string'&&!mapping[photo.original_file_id]&&/^[a-f0-9]{64}$/.test(photo.sha256),'SCHEMA','Некорректный перечень фотографий.');
    var saved=DriveApp.getFileById(photo.backup_file_id);registryPrivate(saved);registryAssert(registryHasParent(saved,archive.getId())&&saved.getDescription()==='pc-arena-backup-photo:v1:'+manifest.source_spreadsheet_id+':'+photo.sha256,'PERMISSION','Фотография находится вне резервного архива.');
    var bytes=saved.getBlob().getBytes();registryAssert(bytes.length===photo.bytes&&bytes.length>0&&bytes.length<=REGISTRY_MAX_PHOTO&&registryHex(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,bytes))===photo.sha256,'SCHEMA','Резервная фотография повреждена.');
    if(!copied[photo.backup_file_id])copied[photo.backup_file_id]=registryPhoto(config,Utilities.base64Encode(bytes));mapping[photo.original_file_id]=copied[photo.backup_file_id];
  });
  var input={guests:source.tables.guests.map(function(g){registryAssert(mapping[g.photo_drive_id],'SCHEMA','В копии отсутствует фотография гостя.');var row=registryWireGuest(sourceConfig,g,false);row.photo_ref=mapping[g.photo_drive_id];return row;}),visits:source.tables.visits,audit_events:source.tables.audit_events,staff:source.tables.staff,employee_access:source.tables.employee_access,visit_reminders:source.tables.visit_reminders};
  var result=registryImport(config,s,{admin_id:REGISTRY_ADMIN,state:input,sequences:{guests:Number(registryMeta(source,'next_guest_id'))-1,visits:Number(registryMeta(source,'next_visit_id'))-1,audit_events:Number(registryMeta(source,'next_audit_id'))-1}});delete result.imported;result.restored=true;registrySetMeta(s,'restored_from',p.manifest_file_id);return result;
}

function doPost(event) {
  var lock=null;REGISTRY_PRIVATE_CACHE=Object.create(null);
  try {
    var config=registryConfig();
    registryAssert(event&&event.postData&&typeof event.postData.contents==='string','AUTH','Некорректный запрос.');registryAssert(event.postData.contents.length<=16*1024*1024,'LIMIT','Запрос слишком большой; передавайте фотографии по одной.');
    var body=JSON.parse(event.postData.contents);registryAssert(typeof body.payload==='string'&&typeof body.signature==='string'&&/^[a-f0-9]{64}$/.test(body.signature),'AUTH','Подпись запроса отсутствует.');
    var expected=registryHex(Utilities.computeHmacSha256Signature(body.payload,config.REGISTRY_SECRET,Utilities.Charset.UTF_8));registryAssert(registryEqual(expected,body.signature),'AUTH','Неверная подпись запроса.');
    var request=JSON.parse(body.payload);registryAssert(request&&request.version===1&&typeof request.operation_id==='string'&&/^[a-f0-9]{32}$/.test(request.operation_id)&&registryInteger(request.issued_at,0)&&Math.abs(Date.now()/1000-request.issued_at)<=300&&typeof request.action==='string'&&request.params&&typeof request.params==='object'&&!Array.isArray(request.params),'AUTH','Некорректный или просроченный запрос.');
    lock=LockService.getScriptLock();registryAssert(lock.tryLock(20000),'BUSY','Хранилище занято. Повторите действие.');
    var state=registryRead(config,false),write=REGISTRY_WRITES.indexOf(request.action)>=0,hash=registryHash(body.payload),previous=state.tables.operations.find(function(r){return r.operation_id===request.operation_id;});
    if(previous) {registryAssert(previous.payload_hash===hash,'AUTH','Идентификатор операции уже использован.');return registryOutput({ok:true,result:registryMaterialize(config,JSON.parse(previous.result_json))});}
    var result=registryDispatch(config,state,request.action,request.params);
    if(write) {state.tables.operations.push({operation_id:request.operation_id,payload_hash:hash,result_json:JSON.stringify(result),created_at:registryMoment()});state.dirty.operations=true;registryValidateState(state);registryCommit(config,state);}
    return registryOutput({ok:true,result:registryMaterialize(config,result)});
  } catch(error) {
    var safeCodes=['AUTH','CONFIG','SCHEMA','LIMIT','VALIDATION','PERMISSION','DUPLICATE_PHONE','STALE_GUEST','VISIT_STATE','CONFLICT','BUSY'];
    var known=error&&safeCodes.indexOf(error.registryCode)>=0;
    return registryOutput({ok:false,error:{code:known?error.registryCode:'STORAGE',message:known?error.message:'Не удалось обратиться к хранилищу. Попробуйте позже.'}});
  } finally {REGISTRY_PRIVATE_CACHE=null;if(lock)lock.releaseLock();}
}
function doGet() {return registryOutput({ok:false,error:{code:'AUTH',message:'Используйте подписанный запрос.'}});}
function registryOutput(value) {return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);}
