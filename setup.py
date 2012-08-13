from setuptools import setup
import os

absolute_path = lambda x: os.path.join(os.path.dirname(__file__), x) 
readme_path = absolute_path(u'README.rst')

setup(
    name = "django-template-preprocessor",
    version='1.2.27',
    url = 'https://github.com/citylive/django-template-preprocessor',
    license = 'BSD',
    description = "Template preprocessor/compiler for Django",
    long_description = open(readme_path, 'r').read(),
    author = 'Jonathan Slenders, City Live nv',
    packages = ['template_preprocessor'], #find_packages('src', exclude=['*.test_project', 'test_project', 'test_project.*', '*.test_project.*']),
    package_dir = {'': 'src'},
    package_data = {'template_preprocessor': [
        'templates/*.html', 'templates/*/*.html', 'templates/*/*/*.html',
        'static/*/js/*.js', 'static/*/css/*.css',
        ],},
    include_package_data=True,
    zip_safe=False, # Don't create egg files, Django cannot find templates in egg files.
    classifiers = [
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Programming Language :: Python',
        'Operating System :: OS Independent',
        'Environment :: Web Environment',
        'Framework :: Django',
        'Topic :: Software Development :: Internationalization',
    ],
)

