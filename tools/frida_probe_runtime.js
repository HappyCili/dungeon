/**
 * 探测 dungeon4 运行时：IL2CPP 程序集、Puerts、网络导出符号。
 */
'use strict';

function log(m) { console.log('[probe] ' + m); }

function listExports(modName, needles) {
  var mod = Process.findModuleByName(modName);
  if (!mod) {
    log('module missing: ' + modName);
    return;
  }
  log('module ' + modName + ' base=' + mod.base + ' size=' + mod.size);
  var n = 0;
  mod.enumerateExports().forEach(function (e) {
    var name = e.name || '';
    for (var i = 0; i < needles.length; i++) {
      if (name.toLowerCase().indexOf(needles[i].toLowerCase()) >= 0) {
        log('  export ' + e.type + ' ' + name + ' @ ' + e.address);
        n++;
        break;
      }
    }
  });
  log('  matched exports: ' + n);
}

function main() {
  log('modules containing il2cpp/puerts/unity/socket:');
  Process.enumerateModules().forEach(function (m) {
    var n = m.name.toLowerCase();
    if (
      n.indexOf('il2cpp') >= 0 ||
      n.indexOf('puerts') >= 0 ||
      n.indexOf('tuanjie') >= 0 ||
      n.indexOf('unity') >= 0 ||
      n.indexOf('websocket') >= 0 ||
      n.indexOf('libmain') >= 0
    ) {
      log('  ' + m.name + ' @ ' + m.base);
    }
  });

  listExports('libil2cpp.so', ['il2cpp_class', 'il2cpp_domain', 'il2cpp_runtime']);
  listExports('libpuerts.so', ['Eval', 'Execute', 'JS', 'V8', 'Quick', 'Isolate', 'pesapi', 'Puerts']);
  listExports('libunity.so', ['recv', 'send', 'websocket']);
  listExports('libmain.so', ['JNI', 'native']);

  if (typeof Il2Cpp !== 'undefined') {
    Il2Cpp.perform(function () {
      log('IL2CPP domain assemblies:');
      var count = 0;
      var hit = 0;
      Il2Cpp.domain.assemblies.forEach(function (asm) {
        count++;
        var an = asm.name;
        if (
          an.indexOf('CSharp') >= 0 ||
          an.indexOf('Game') >= 0 ||
          an.indexOf('Assembly') >= 0 ||
          an.indexOf('Puerts') >= 0
        ) {
          log('  asm ' + an);
        }
        try {
          asm.image.classes.forEach(function (klass) {
            var full = (klass.namespace ? klass.namespace + '.' : '') + klass.name;
            var fl = full.toLowerCase();
            if (
              fl.indexOf('socket') >= 0 ||
              fl.indexOf('battle') >= 0 ||
              fl.indexOf('puerts') >= 0 ||
              fl.indexOf('websocket') >= 0 ||
              fl.indexOf('pack1') >= 0 ||
              fl.indexOf('network') >= 0
            ) {
              hit++;
              if (hit <= 80) log('  class ' + full + ' [' + an + ']');
            }
          });
        } catch (e) {}
      });
      log('assemblies=' + count + ' interesting_classes~=' + hit);
    });
  } else {
    log('Il2Cpp bridge not loaded');
  }
}

setImmediate(main);
