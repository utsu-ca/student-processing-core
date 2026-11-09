# AWS Fargate Module

variable "region" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "image_tag" {
  type = string
}

variable "services" {
  type = list(string)
}

variable "cpu" {
  type = string
}

variable "memory" {
  type = string
}

variable "aws_account_id" {
  type = string
}

# ECS Cluster
resource "aws_ecs_cluster" "main" {
  name = var.cluster_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# VPC and Networking
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${var.cluster_name}-vpc"
  }
}

resource "aws_subnet" "main" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.${count.index + 1}.0/24"
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = {
    Name = "${var.cluster_name}-subnet-${count.index + 1}"
  }
}

resource "aws_security_group" "alb" {
  name   = "${var.cluster_name}-alb"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "ecs_tasks" {
  name   = "${var.cluster_name}-ecs-tasks"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port       = 8000
    to_port         = 8002
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Application Load Balancer
resource "aws_lb" "main" {
  name               = "${var.cluster_name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.main[*].id
}

resource "aws_lb_target_group" "main" {
  for_each = toset(var.services)
  
  name        = "${each.value}-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    healthy_threshold   = 2
    unhealthy_threshold = 2
    timeout             = 3
    interval            = 30
    path                = "/health"
    matcher             = "200"
  }
}

# IAM Role for ECS Task Execution
resource "aws_iam_role" "ecs_task_execution_role" {
  name = "${var.cluster_name}-ecs-task-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_role_policy" {
  role       = aws_iam_role.ecs_task_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/ecs/${var.cluster_name}"
  retention_in_days = 7
}

# ECS Task Definition
resource "aws_ecs_task_definition" "main" {
  for_each = toset(var.services)
  
  family                   = each.value
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_task_execution_role.arn

  container_definitions = jsonencode([
    {
      name      = each.value
      image     = "${var.aws_account_id}.dkr.ecr.${var.region}.amazonaws.com/${each.value}:${var.image_tag}"
      essential = true
      portMappings = [
        {
          containerPort = 8000
          hostPort      = 8000
          protocol      = "tcp"
        }
      ]

      environment = [
        {
          name  = "PORTAL_SERVICE_TOKEN"
          value = random_password.service_token.result
        },
        {
          name  = "DATABASE_URL"
          value = "postgresql://${aws_rds_cluster.db.master_username}:${random_password.db_password.result}@${aws_rds_cluster.db.endpoint}:5432/utsudb"
        }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])
}

# ECS Service
resource "aws_ecs_service" "main" {
  for_each = toset(var.services)
  
  name            = each.value
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.main[each.key].arn
  desired_count   = 2
  launch_type     = "FARGATE"

  network_configuration {
    security_groups  = [aws_security_group.ecs_tasks.id]
    subnets          = aws_subnet.main[*].id
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.main[each.key].arn
    container_name   = each.value
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.main]
}

# ALB Listener
resource "aws_lb_listener" "main" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.main["portal"].arn
  }
}

# RDS PostgreSQL
resource "aws_rds_cluster" "db" {
  cluster_identifier     = "${var.cluster_name}-db"
  engine                 = "aurora-postgresql"
  engine_version         = "14.6"
  database_name          = "utsudb"
  master_username        = "utsuadmin"
  master_password        = random_password.db_password.result
  db_subnet_group_name   = aws_db_subnet_group.main.name
  skip_final_snapshot    = true
  publicly_accessible    = false

  depends_on = [aws_internet_gateway.main]
}

resource "aws_rds_cluster_instance" "db" {
  count              = 2
  cluster_identifier = aws_rds_cluster.db.id
  instance_class     = "db.t3.small"
  engine              = aws_rds_cluster.db.engine
  engine_version      = aws_rds_cluster.db.engine_version
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.cluster_name}-db-subnet"
  subnet_ids = aws_subnet.main[*].id
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.cluster_name}-igw"
  }
}

resource "random_password" "service_token" {
  length  = 32
  special = true
}

resource "random_password" "db_password" {
  length  = 24
  special = true
}

data "aws_availability_zones" "available" {
  state = "available"
}

# Outputs
output "portal_url" {
  value = "http://${aws_lb.main.dns_name}"
}

output "authsvc_url" {
  value = "http://authsvc-ecs.${var.region}.compute.internal"
}

output "mailsvc_url" {
  value = "http://mailsvc-ecs.${var.region}.compute.internal"
}
