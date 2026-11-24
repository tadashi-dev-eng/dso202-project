// Jenkinsfile -- TaskFlow CI (DSO202 final project)
//
//   commit on main
//     -> Plan      which service images changed since the last green build
//     -> Test      unit tests (pytest) + chart and overlay validation
//     -> Build     Kaniko builds each changed image, tag = short commit SHA,
//                  pushes to the cluster registry
//     -> Scan      Trivy fails the build on fixable CRITICAL vulnerabilities
//     -> Deploy    commits the new tags to charts/taskflow/values-dev.yaml
//                  ("[skip ci]"); Argo CD notices the commit and syncs dev
//     -> Verify    waits for the dev rollout, runs the end-to-end smoke test
//     -> Promote   (manual, PROMOTE_TO_PROD) copies dev's tags to prod after
//                  an approval, then verifies prod the same way
//
// CI never applies anything to the cluster: it only writes Git. Argo CD is
// the only thing that deploys (pull-based GitOps), and a rollback is a
// `git revert` of the tag commit.

pipeline {
  agent {
    kubernetes {
      yamlFile 'ci/agent-pod.yaml'
      defaultContainer 'tools'
    }
  }
  options {
    buildDiscarder(logRotator(numToKeepStr: '20'))
    timeout(time: 60, unit: 'MINUTES')
    disableConcurrentBuilds()
    timestamps()
  }
  parameters {
    booleanParam(name: 'BUILD_ALL', defaultValue: false, description: 'Rebuild every image')
    booleanParam(name: 'PROMOTE_TO_PROD', defaultValue: false, description: 'Promote the images running in dev to prod')
  }
  environment {
    REGISTRY_PUSH = 'kind-registry:5000'          // registry as seen from Pods
    REGISTRY_PULL = 'localhost:5001'              // the same registry as kind nodes pull it
    REPO = 'github.com/tadashi-dev-eng/dso202-project.git'
    INGRESS = 'https://ingress-nginx-controller.ingress-nginx.svc'
  }

  stages {
    stage('Plan') {
      steps {
        script {
          env.SHORT_SHA = sh(script: 'git rev-parse --short=7 HEAD', returnStdout: true).trim()
          def message = sh(script: 'git log -1 --pretty=%B', returnStdout: true)
          env.SKIP = (message.contains('[skip ci]') && !params.PROMOTE_TO_PROD) ? 'true' : 'false'
          def base = params.BUILD_ALL ? '' : (env.GIT_PREVIOUS_SUCCESSFUL_COMMIT ?: '')
          env.CHANGED = sh(script: "python ci/changed_services.py ${base}", returnStdout: true).trim()
          echo "commit ${env.SHORT_SHA}; skip=${env.SKIP}; services to build: ${env.CHANGED ?: '(none)'}"
        }
      }
    }

    stage('Test') {
      when { expression { env.SKIP != 'true' } }
      parallel {
        stage('Unit tests') {
          steps {
            sh '''
              cd services
              REGISTRY_ENABLED=false PYTHONPATH=common \
                python -m pytest -q -p no:cacheprovider --junitxml=../unit-tests.xml tests
            '''
          }
          post { always { junit 'unit-tests.xml' } }
        }
        stage('Charts and overlays') {
          steps {
            sh '''
              for env in dev prod; do
                helm lint charts/taskflow -f charts/taskflow/values-$env.yaml
                helm lint charts/taskflow-platform -f charts/taskflow-platform/values-$env.yaml
                helm template t charts/taskflow -f charts/taskflow/values-$env.yaml > /dev/null
                kubectl kustomize deploy/environments/overlays/$env > /dev/null
              done
              kubectl kustomize deploy/argocd/bootstrap > /dev/null
            '''
          }
        }
      }
    }

    stage('Build') {
      when { expression { env.SKIP != 'true' && env.CHANGED } }
      steps {
        container('kaniko') {
          script {
            for (svc in env.CHANGED.split(' ')) {
              def context = svc == 'web-frontend' ? 'services/web-frontend' : 'services'
              def dockerfile = svc == 'web-frontend' ? 'services/web-frontend/Dockerfile' : 'services/Dockerfile'
              sh """
                /kaniko/executor --context=dir://\$WORKSPACE/${context} \
                  --dockerfile=\$WORKSPACE/${dockerfile} \
                  --build-arg SERVICE=${svc} --build-arg VERSION=${env.SHORT_SHA} \
                  --destination=${env.REGISTRY_PUSH}/taskflow/${svc}:${env.SHORT_SHA} \
                  --insecure --skip-tls-verify --cache=true \
                  --cache-repo=${env.REGISTRY_PUSH}/taskflow/cache \
                  --label org.opencontainers.image.revision=${env.GIT_COMMIT} \
                  --cleanup
              """
            }
          }
        }
      }
    }

    stage('Scan') {
      when { expression { env.SKIP != 'true' && env.CHANGED } }
      steps {
        container('trivy') {
          script {
            for (svc in env.CHANGED.split(' ')) {
              sh """
                trivy image --insecure --no-progress --scanners vuln \
                  --severity CRITICAL --ignore-unfixed --exit-code 1 \
                  ${env.REGISTRY_PUSH}/taskflow/${svc}:${env.SHORT_SHA}
              """
            }
          }
        }
      }
    }

    stage('Deploy to dev (GitOps)') {
      when { expression { env.SKIP != 'true' && env.CHANGED } }
      steps {
        script {
          for (svc in env.CHANGED.split(' ')) {
            sh "python scripts/set_image_tag.py charts/taskflow/values-dev.yaml ${svc} ${env.SHORT_SHA}"
          }
          gitPush("ci: deploy ${env.CHANGED.replace(' ', ', ')} ${env.SHORT_SHA} to dev [skip ci]")
        }
      }
    }

    stage('Verify dev') {
      when { expression { env.SKIP != 'true' && env.CHANGED } }
      steps {
        script { waitForRollout('taskflow-dev', env.CHANGED, env.SHORT_SHA) }
        sh "python scripts/smoke_test.py --base ${INGRESS} --host dev.taskflow.localhost --insecure"
      }
    }

    stage('Promote to prod') {
      when { expression { params.PROMOTE_TO_PROD } }
      steps {
        input message: 'Promote the images running in dev to prod?', ok: 'Promote'
        script {
          sh 'python scripts/set_image_tag.py --promote dev prod'
          gitPush("ci: promote dev images to prod (build ${env.BUILD_NUMBER}) [skip ci]")
          def services = sh(script: "python -c \"import sys; sys.path.insert(0,'scripts'); import set_image_tag as s, pathlib; print(' '.join(s.pinned_tags(pathlib.Path('charts/taskflow/values-prod.yaml'))))\"", returnStdout: true).trim()
          waitForRollout('taskflow-prod', services, null)
        }
        sh "python scripts/smoke_test.py --base ${INGRESS} --host app.taskflow.localhost --insecure"
      }
    }
  }

  post {
    success { echo "TaskFlow ${env.SHORT_SHA}: pipeline green" }
    failure { echo "TaskFlow ${env.SHORT_SHA}: pipeline FAILED -- dev was not changed unless the Deploy stage ran" }
  }
}

// Commit the working tree's values changes and push them to main.
def gitPush(String message) {
  withCredentials([usernamePassword(credentialsId: 'github-push',
                                    usernameVariable: 'GIT_USER', passwordVariable: 'GIT_TOKEN')]) {
    sh """
      git config user.name 'TaskFlow CI'
      git config user.email 'ci@taskflow.localhost'
      git add charts/taskflow/values-*.yaml
      if git diff --cached --quiet; then echo 'nothing to commit'; exit 0; fi
      git commit -m "${message}"
      git push "https://\$GIT_USER:\$GIT_TOKEN@${env.REPO}" HEAD:main
    """
  }
}

// Wait until Argo CD has rolled the given services out with the new tag.
def waitForRollout(String namespace, String services, String tag) {
  timeout(time: 10, unit: 'MINUTES') {
    for (svc in services.split(' ')) {
      if (tag) {
        waitUntil(initialRecheckPeriod: 5000) {
          def image = sh(script: "kubectl -n ${namespace} get deploy ${svc} -o jsonpath='{.spec.template.spec.containers[0].image}'",
                         returnStdout: true).trim()
          echo "${svc}: ${image}"
          return image.endsWith(":${tag}")
        }
      }
      sh "kubectl -n ${namespace} rollout status deploy/${svc} --timeout=300s"
    }
  }
}
