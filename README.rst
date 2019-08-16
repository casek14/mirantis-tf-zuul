How to run
----------

Clone the repo::

    # git clone  https://github.com/casek14/mirantis-tf-zuul.git


Navigate to directory::

    # cd mirantis-tf-zuul/doc/source/admin/examples

Make sure that docker-compose is installed, and run::

    # docker-compose up -d

Now all needed containers should be started. Wait few minutes, so scripts will configure the env.
If everything is up, you should be able to see Gerrit and Zuul webui on url::

    Gerrit: http://localhost:8080/
    Zuul: http://localhost:9000/t/opencontrail/status

Upload TF-project configuration:
-------------------------------

Clone the configuration project for zuul from local gerrit::

    # git clone http://localhost:8080/Juniper/contrail-project-config

Then clone ready configuraton for example to tmp dir::

    # git clone https://github.com/casek14/contrail-project-config.git /tmp/

Then copy all files from the ready configuration repo except .git files to project cloned from local repository::

    # copy /tmp/contrail-project-config/playbooks .
    # copy /tmp/contrail-project-config/roles .
    # copy /tmp/contrail-project-config/zuul.d .

Now you can commit all the changes and push into local contrail-project-config. Then you have to navigate to Gerrit webUI and log in as admin. Gerrit is running in unprotected mode
so it do not require password. Merge the change you uploaded. Then you can check the Zuul webUI, configuration should be reloaded automatically.

Zuul
====

Zuul is a project gating system.

The latest documentation for Zuul v3 is published at:
https://zuul-ci.org/docs/zuul/

If you are looking for the Edge routing service named Zuul that is
related to Netflix, it can be found here:
https://github.com/Netflix/zuul

If you are looking for the Javascript testing tool named Zuul, it
can be found here:
https://github.com/defunctzombie/zuul

Getting Help
------------

There are two Zuul-related mailing lists:

`zuul-announce <http://lists.zuul-ci.org/cgi-bin/mailman/listinfo/zuul-announce>`_
  A low-traffic announcement-only list to which every Zuul operator or
  power-user should subscribe.

`zuul-discuss <http://lists.zuul-ci.org/cgi-bin/mailman/listinfo/zuul-discuss>`_
  General discussion about Zuul, including questions about how to use
  it, and future development.

You will also find Zuul developers in the `#zuul` channel on Freenode
IRC.

Contributing
------------

To browse the latest code, see: https://opendev.org/zuul/zuul
To clone the latest code, use `git clone https://opendev.org/zuul/zuul`

Bugs are handled at: https://storyboard.openstack.org/#!/project/zuul/zuul

Suspected security vulnerabilities are most appreciated if first
reported privately following any of the supported mechanisms
described at https://zuul-ci.org/docs/zuul/user/vulnerabilities.html

Code reviews are handled by gerrit at https://review.openstack.org

After creating a Gerrit account, use `git review` to submit patches.
Example::

    # Do your commits
    $ git review
    # Enter your username if prompted

Join `#zuul` on Freenode to discuss development or usage.

License
-------

Zuul is free software.  Most of Zuul is licensed under the Apache
License, version 2.0.  Some parts of Zuul are licensed under the
General Public License, version 3.0.  Please see the license headers
at the tops of individual source files.

Python Version Support
----------------------

Zuul v3 requires Python 3. It does not support Python 2.

As Ansible is used for the execution of jobs, it's important to note that
while Ansible does support Python 3, not all of Ansible's modules do. Zuul
currently sets ``ansible_python_interpreter`` to python2 so that remote
content will be executed with Python 2.
