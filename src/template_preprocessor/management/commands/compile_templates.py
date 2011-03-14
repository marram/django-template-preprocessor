"""
Author: Jonathan Slenders, City Live
"""
import os
import codecs
from optparse import make_option
import termcolor

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.urlresolvers import reverse
from django.template import TemplateDoesNotExist

from template_preprocessor.core import compile
from template_preprocessor.core.lexer import CompileException

from template_preprocessor.utils import language, template_iterator, load_template_source



class Command(BaseCommand):
    help = "Preprocess all the templates form all known applications."
    option_list = BaseCommand.option_list + (
        make_option('--language', action='append', dest='languages', help='Give the languages'),
        make_option('--all', action='store_true', dest='all_templates', help='Compile all templates (instead of only the changed)'),
    )


    def print_error(self, text):
        self._errors.append(text)
        print termcolor.colored(text, 'white', 'on_red')


    def handle(self, *args, **options):
        all_templates = options['all_templates']

        # Default verbosity
        self.verbosity = int(options.get('verbosity', 1))

        # All languages by default
        languages = [l[0] for l in settings.LANGUAGES]
        if options['languages'] is None:
            options['languages'] = languages


        self._errors = []
        if languages.sort() != options['languages'].sort():
            print termcolor.colored('Warning: all template languages are deleted while we won\'t generate them again.', 'white', 'on_red')

        # Delete previously compiled templates
        # (This is to be sure that no template loaders were configured to
        # load files from this cache.)
        if all_templates:
            for root, dirs, files in os.walk(settings.TEMPLATE_CACHE_DIR):
                for f in files:
                    path = os.path.join(root, f)
                    if self.verbosity >= 1:
                        print ('Deleting old template: %s' % path)
                    os.remove(path)

        # Create compile queue
        queue = set()

        if self.verbosity >= 2:
            print 'Building queue'

        for lang in options['languages']:
            # Now compile all templates to the cache directory
            for dir, t in template_iterator():
                input_path = os.path.join(dir, t)
                output_path = os.path.join(settings.TEMPLATE_CACHE_DIR, lang, t)
                if (
                        all_templates or
                        not os.path.exists(output_path) or
                        os.path.getmtime(output_path) < os.path.getmtime(input_path)):
                    queue.add( (lang, input_path, output_path) )

        queue = list(queue)
        queue.sort()

        for i in range(0, len(queue)):
            lang = queue[i][0]
            with language(lang):
                if self.verbosity >= 2:
                    print termcolor.colored('%i / %i |' % (i, len(queue)), 'yellow'),
                    print termcolor.colored('(%s)' % lang, 'yellow'),
                    print termcolor.colored(queue[i][1], 'green')

                self._compile_template(*queue[i])

        # Show all errors once again.
        print u'\n*** %i Files processed, %i compile errors ***' % (len(queue), len(self._errors))

    def _compile_template(self, lang, input_path, output_path):
        try:
            # Open input file
            code = codecs.open(input_path, 'r', 'utf-8').read()

            # Compile
            output = compile(code, loader=load_template_source, path=input_path)

            # Open output file
            self._create_dir(os.path.split(output_path)[0])
            codecs.open(output_path, 'w', 'utf-8').write(output)

        except CompileException, e:
            self.print_error(u'ERROR:  %s' % unicode(e))

        except TemplateDoesNotExist, e:
            if self.verbosity >= 2:
                print u'WARNING: Template does not exist:  %s' % unicode(e)

    def _create_dir(self, newdir):
        if not os.path.isdir(newdir):
            os.makedirs(newdir)
