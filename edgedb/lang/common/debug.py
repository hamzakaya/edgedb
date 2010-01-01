import inspect
import re
import ast

from semantix.utils.functional import decorate

enabled = False
channels = set()

class DebugDecoratorParseError(Exception): pass

def _indent_code(source, absolute=None, relative=None):
    def _calc_tab_size(str):
        count = 0
        for i in str:
            if i == ' ':
                count += 1
            else:
                break
        return count

    tab_size = min(_calc_tab_size(line) for line in source.split('\n') if line.strip())

    if relative is not None:
        absolute = tab_size + relative

    if absolute < 0:
        absolute = 0

    if absolute is not None:
        source = '\n'.join([(' ' * absolute) + line[tab_size:] \
                          for line in source.split('\n')])

    return source

def _set_location(node, lineno):
    if 'lineno' in node._attributes:
        node.lineno = lineno

    for c in ast.iter_child_nodes(node):
        _set_location(c, lineno)
    return node

class debug(object):
    active = False

    def __new__(cls, func):
        if cls.active or not enabled:
            return func

        cls.active = True

        source = _indent_code(inspect.getsource(func), absolute=0)
        sourceloc = inspect.getsourcelines(func)[1]
        orig_file = inspect.getsourcefile(func)

        tree = ast.parse(source, filename=orig_file)
        ast.increment_lineno(tree, sourceloc - 1)

        class Transformer(ast.NodeTransformer):

            pattern = re.compile(r'''LOG
                                     \s+ \[ \s* (?P<tags> [\w\.]+ (?:\s* , \s* [\w+\.]+)* ) \s* \]
                                     (?P<title>.*)
                                  ''', re.X)

            def visit_Expr(self, node):
                if isinstance(node.value, ast.Str):
                    if node.value.s.startswith('LOG'):
                        m = Transformer.pattern.match(node.value.s)
                        if m:
                            title = m.group('title').strip()
                            tags = {t.strip() for t in m.group('tags').split(',')}

                            comment = node.value.s.split('\n')

                            # Str().lineno is for the _last_ line of the string.
                            # We want to use the first.
                            lineno = node.lineno - len(comment) + 1

                            text = 'import semantix.utils.debug\n' \
                                   'if semantix.utils.debug.channels & %r:\n' \
                                   '    pass\n' % tags

                            if title:
                                text += '    print("\\n" + "="*80 + "\\n" + %r + "\\n" + "="*80)\n' % title

                            code = ast.parse(text.rstrip(), filename=orig_file)
                            code = ast.fix_missing_locations(code)
                            _set_location(code, lineno)

                            ctext = _indent_code('\n'.join(comment[1:]), absolute=0)
                            ccode = ast.parse(ctext, filename=orig_file)

                            ast.increment_lineno(ccode, lineno)

                            # Prepend the custom code to the If block body
                            code.body[1].body.extend(ccode.body)

                            return code.body
                        else:
                            raise DebugDecoratorParseError('invalid debug decorator syntax')
                return node

        tree = Transformer().visit(tree)
        code = compile(tree, orig_file if orig_file else '<string>', 'exec')

        _locals = {}
        exec(code, func.__globals__, _locals)

        new_func = _locals[func.__name__]
        cls.active = False

        decorate(new_func, func)

        return new_func

def highlight(code, lang=None):
    try:
        from pygments import highlight as h
        from pygments.lexers import get_lexer_by_name
        from pygments.formatters import Terminal256Formatter
    except ImportError:
        return code

    return h(code, get_lexer_by_name(lang), Terminal256Formatter(bg='dark', style='native'))
