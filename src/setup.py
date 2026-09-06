from setuptools import setup
import setup_translate

pkg = 'Extensions.Telerising'
setup(name='enigma2-plugin-extensions-telerising',
       version='0.1.0',
       description='Local Telerising TV server and Enigma2 channel import',
       package_dir={pkg: 'Telerising'},
       packages=[pkg],
       package_data={pkg: ["*.xml", "*.png", "images/*.png", "locale/*/LC_MESSAGES/*.mo", "web/*.html"]},
       cmdclass=setup_translate.cmdclass,  # for translation
      )
