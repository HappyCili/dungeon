/**
 * Read-only Puerts runtime probe.
 *
 * Lists the managed JavaScript environment classes and their methods so a
 * client-session automation bridge can use the game's own Stage and socket.
 */
'use strict';

function log(message) {
  console.log('[puerts-probe] ' + message);
}

function isRelevant(klass) {
  var full = ((klass.namespace ? klass.namespace + '.' : '') + klass.name).toLowerCase();
  return (
    full.indexOf('puerts') >= 0 ||
    full.indexOf('jsenv') >= 0 ||
    full.indexOf('javascriptenv') >= 0 ||
    full.indexOf('scriptengine') >= 0
  );
}

function methodSignature(method) {
  try {
    var parameters = method.parameters.map(function (parameter) {
      return parameter.type.name;
    });
    return method.name + '(' + parameters.join(', ') + ')';
  } catch (_) {
    return method.name + '(?)';
  }
}

function main() {
  if (typeof Il2Cpp === 'undefined') {
    log('frida-il2cpp-bridge is required');
    return;
  }

  Il2Cpp.perform(function () {
    var found = 0;
    Il2Cpp.domain.assemblies.forEach(function (assembly) {
      assembly.image.classes.forEach(function (klass) {
        if (!isRelevant(klass)) return;
        found += 1;
        var full = (klass.namespace ? klass.namespace + '.' : '') + klass.name;
        log('class ' + full + ' [' + assembly.name + ']');
        klass.methods.forEach(function (method) {
          var name = method.name.toLowerCase();
          if (
            name.indexOf('eval') >= 0 ||
            name.indexOf('execute') >= 0 ||
            name.indexOf('tick') >= 0 ||
            name.indexOf('dispose') >= 0 ||
            name.indexOf('env') >= 0
          ) {
            log('  ' + methodSignature(method));
          }
        });
      });
    });
    log('matched classes=' + found);
  });
}

setImmediate(main);
